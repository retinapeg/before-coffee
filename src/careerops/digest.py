"""The morning digest: one plain email listing jobs worth applying to.

Scope, deliberately narrow: this module SELECTS and FORMATS. It does not collect, it
does not filter the database, and it writes exactly one thing back — a ledger of what
it has already sent, so tomorrow's email does not repeat today's.

Four decisions that are not obvious:

**"Meets your criteria" reads the evidence band, not `candidacy.recommended`.**
On the private system's store `recommended` was False for every job, because the
recommendation gate closes on any uncertainty and almost every advert carries some.
Reading it would produce a permanently empty digest. `fit_band` says whether the
candidate's evidence supports the role, which is the question a digest is asking.

**The ledger decides what is new, not a timestamp.** The brief asked for "jobs first
seen since the last digest". Taken literally alongside a 25-a-day cap that is a data
loss: on one evening, measured on the private system's store (not reproducible
from this repository), 156 roles qualified and 131 were held back by the cap, and
every one of them was first seen BEFORE the digest that could not fit them. A time floor would
discard all 131 permanently, while the email itself promises they "will appear in the
next digest". The ledger of sent job ids excludes exactly what has been shown and
nothing more, which is what "since the last digest" was reaching for. The floor is
gone; the ledger is the only gate.

**Age comes only from `posted_at`.** A standing project rule: "First seen is never a
posting date."
`first_seen` decides what is NEW TO US and nothing else. A job whose employer
published no date is listed and labelled "posting date not published" rather than
being quietly treated as new.

**Two sections, London then International, and each job appears in exactly one.**
Where a role is decides whether it is worth reading at all, so it is the top-level
split. Why a role qualified - including whether it is outside the searches the owner
has configured - is kept as each job's "Why:" line rather than as a heading.

**Geography is part of "meets your criteria".** The evidence band says nothing about
where a role is, so without this the digest offered Shanghai, Mexico City and Montreal
to someone searching London plus a few configured countries. The test uses the
existing classifier, `inventory.classify_job`,
rather than a new rule of its own: a role qualifies on location if it is London or a
London alternative, or if any of its countries is one the owner has enabled in
settings. Turn it off with `digest.require_configured_location = false`.

**No prediction, ever.** No match percentage, no score, no ranking number, no claim
about whether an employer will reply. The email states the reason a job was selected
and leaves the judgement to the reader.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from email.message import EmailMessage

from . import signals

SENT_KEY = "digest_sent"
SETTINGS_KEY = "digest"
MAX_JOBS = 25
FIT_BANDS = {"strong", "plausible"}

# Strongest reason first. A job is listed under the first heading that applies.
# The email's two sections, in printing order. Each job appears in exactly one.
# Roles outside every configured country are excluded before this point.
SECTIONS = (
    ("london", "London"),
    ("international", "International"),
)

# Why a job qualified, strongest first. Printed as each job's "Why:" line.
GROUPS = (
    ("adjacent", "Outside the searches you have configured"),
    ("fresh", "Posted in the last day"),
    ("source", "On the employer's own board"),
    ("criteria", "Meets your configured criteria"),
)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Apply links carry long tracking parameters and a shortened one is a broken one. The
# limit only bounds pathological input; control characters are removed either way.
URL_LIMIT = 2048


def _clean(value, limit: int = 200) -> str:
    """Advert text is untrusted third-party input. Collapse it to one safe line."""
    text = _CONTROL.sub(" ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def settings(store) -> dict:
    configured = store.settings().get(SETTINGS_KEY)
    configured = configured if isinstance(configured, dict) else {}
    return {
        "to": str(configured.get("to") or "").strip(),
        "fresh_hours": int(configured.get("fresh_hours") or signals.DEFAULT_FRESH_HOURS),
        "limit": max(1, min(MAX_JOBS, int(configured.get("limit") or MAX_JOBS))),
        # Sending anywhere other than the authenticated mailbox has to be turned on
        # deliberately. The default makes "this app messages no third party" structural.
        "allow_other_recipient": configured.get("allow_other_recipient") is True,
        "require_configured_location": configured.get("require_configured_location") is not False,
    }


def ledger(store) -> dict:
    stored = store.meta(SENT_KEY, {})
    stored = stored if isinstance(stored, dict) else {}
    ids = stored.get("job_ids")
    return {"job_ids": [i for i in (ids if isinstance(ids, list) else [])],
            "last_sent_at": stored.get("last_sent_at"),
            "sends": int(stored.get("sends") or 0),
            "last_message_id": stored.get("last_message_id")}


def record_sent(store, job_ids, *, now=None, message_id=None) -> dict:
    """Remember what went out, so tomorrow's digest does not repeat it."""
    current = ledger(store)
    already = list(current["job_ids"])
    seen = set(already)
    already.extend(i for i in job_ids if i not in seen)
    updated = {"job_ids": already,
               "last_sent_at": (now or datetime.now(timezone.utc)).isoformat(),
               "sends": current["sends"] + 1,
               "last_message_id": message_id}
    store.put_meta(SENT_KEY, updated)
    return updated


def select(store, *, now=None, limit=None, fresh_hours=None, include_already_sent=False) -> dict:
    """The jobs for this digest: new since the last one, meeting criteria, newest first.

    Returns the groups in printing order plus the counts behind them, so the caller
    can report what it decided without recomputing anything.
    """
    now = now or datetime.now(timezone.utc)
    configured = settings(store)
    window = fresh_hours if fresh_hours is not None else configured["fresh_hours"]
    cap = limit if limit is not None else configured["limit"]

    sent = ledger(store)
    already = set() if include_already_sent else set(sent["job_ids"])
    # Reported in the email, never used to filter. See the module docstring: a time
    # floor plus a daily cap loses every job the cap held back.
    since = _parse(sent["last_sent_at"])

    store_settings = store.settings()
    terms = signals.searched_terms(store_settings)
    source_types = _source_types(store)

    # Upper case, because classify_job reports country codes in upper case.
    enabled_countries = {str(code).upper()
                         for code, value in (store_settings.get("locations") or {}).items()
                         if (value or {}).get("enabled")}
    location_gate = configured["require_configured_location"] and bool(enabled_countries)

    considered = excluded_sent = excluded_band = excluded_location = 0
    rows = []
    for job in store.jobs():
        if job.get("hidden") or job.get("duplicate_of"):
            continue
        considered += 1
        if job.get("id") in already:
            excluded_sent += 1
            continue
        candidacy = (job.get("evaluation") or {}).get("candidacy") or {}
        if str(candidacy.get("fit_band") or "") not in FIT_BANDS:
            excluded_band += 1
            continue
        region = _region(job, store_settings, enabled_countries)
        if location_gate and region is None:
            excluded_location += 1
            continue
        marks = signals.evaluate(job, store_settings, now=now, fresh_hours=window,
                                 source_type=source_types.get(job.get("id")), searched=terms)
        rows.append({"job": job, "signals": marks, "reason": _reason(marks),
                     "region": region or "international"})

    rows.sort(key=_newest_first)
    chosen, overflow = rows[:cap], max(0, len(rows) - cap)

    groups = []
    for key, heading in SECTIONS:
        members = [r for r in chosen if r["region"] == key]
        if members:
            groups.append({"key": key, "heading": heading, "rows": members})

    return {"now": now.isoformat(), "since": since.isoformat() if since else None,
            "fresh_hours": window, "limit": cap, "groups": groups, "rows": chosen,
            "counts": {"considered": considered, "qualifying": len(rows),
                       "selected": len(chosen), "held_back": overflow,
                       "already_sent": excluded_sent, "below_band": excluded_band,
                       "outside_configured_locations": excluded_location},
            "first_digest": since is None}


def _region(job: dict, store_settings: dict, enabled: set) -> str | None:
    """"london", "international", or None when outside every configured location.

    London means London or a London alternative; international means any country the
    owner has enabled. Uses inventory.classify_job so the digest agrees with the rest
    of the product about where a job is, instead of parsing locations a second way.
    """
    from .inventory import classify_job
    try:
        classified = classify_job(job, store_settings)
    except Exception:  # noqa: BLE001 - never drop a job because the classifier tripped
        return "international"
    if classified.get("region") == "london" or classified.get("london_alternative"):
        return "london"
    if set(classified.get("all_countries") or []) & enabled:
        return "international"
    return None


def _source_types(store) -> dict:
    """Which feed each job arrived through, read from the sources table."""
    import json
    mapping = {}
    with store.connect() as db:
        for row in db.execute("SELECT job_id,data FROM sources"):
            try:
                mapping[row["job_id"]] = (json.loads(row["data"]) or {}).get("type")
            except (TypeError, ValueError):
                continue
    return mapping


def _reason(marks: dict) -> str:
    for key, _ in GROUPS:
        if key == "criteria":
            return "criteria"
        if marks.get(key, {}).get("hit"):
            return key
    return "criteria"


def _newest_first(row):
    posted = _parse(row["job"].get("posted_at"))
    # Newest published first; jobs with no published date last but still included.
    return (0 if posted else 1, -(posted.timestamp() if posted else 0), row["job"].get("id") or 0)


# --- the email -----------------------------------------------------------------------

def _age(job, now: datetime) -> str:
    posted = _parse(job.get("posted_at"))
    if posted is None:
        return "posting date not published"
    hours = (now - posted).total_seconds() / 3600
    if hours < 1:
        return "posted within the hour"
    if hours < 48:
        return "posted %d hours ago" % round(hours)
    return "posted %d days ago" % round(hours / 24)


# Some stored records carry parse artefacts rather than pay: a currency of literally
# "unknown", or a 20-to-20 range for an annual salary. Printing those verbatim produced
# "unknown20 to 20 per annual", which is worse than saying nothing.
_CURRENCY = re.compile(r"^(?:[A-Z]{3}|[£$€¥₪])$")
_IMPLAUSIBLE_ANNUAL = 1000


def _salary(job) -> str:
    text = _clean(job.get("salary_text"), 80)
    if text:
        return text
    amounts = [v for v in (job.get("salary_min"), job.get("salary_max"))
               if isinstance(v, (int, float)) and v > 0]
    if not amounts:
        return ""
    period = _clean(job.get("salary_period"), 12)
    annual = period.casefold() in {"", "annual", "annually", "year", "yearly", "per annum"}
    if annual and max(amounts) < _IMPLAUSIBLE_ANNUAL:
        # Not a salary: an artefact of parsing. Report it as unpublished.
        return ""
    currency = _clean(job.get("salary_currency"), 8)
    prefix = currency if _CURRENCY.match(currency or "") else ""
    span = " to ".join(f"{int(v):,}" for v in amounts)
    suffix = f" per {period}" if period and not annual else ""
    return f"{prefix}{' ' if prefix and len(prefix) == 3 else ''}{span}{suffix}".strip()


def _why(row) -> str:
    """One line, from the deterministic rule that selected it. Never a prediction."""
    marks = row["signals"]
    reasons = {
        "adjacent": "Meets your criteria, and the title is not one your configured searches look for.",
        "fresh": "Meets your criteria and the employer published it in the last %d hours.",
        "source": "Meets your criteria and is listed on the employer's own board rather than an aggregator.",
        "criteria": "Meets your configured criteria on the evidence recorded for you.",
    }
    line = reasons[row["reason"]]
    return line % row.get("fresh_hours", 24) if "%d" in line else line


def render(data: dict) -> tuple[str, str]:
    """Subject and plain-text body. Plain text on purpose: it is readable in any
    client, on a phone, with no images to load and nothing to render."""
    now = _parse(data["now"]) or datetime.now(timezone.utc)
    counts = data["counts"]
    total = counts["selected"]

    subject = ("CareerOps digest: nothing new meets your criteria" if total == 0 else
               "CareerOps digest: 1 role to look at" if total == 1 else
               f"CareerOps digest: {total} roles to look at")

    lines = [f"{total} role{'s' if total != 1 else ''} selected"
             f" from {counts['considered']} vacancies in the database.", ""]

    if data["first_digest"]:
        lines += ["This is the first digest, so it covers everything currently stored."
                  " From now on it lists only roles you have not been shown before.", ""]
    elif data["since"]:
        since = _parse(data["since"])
        lines += ["Roles you have not been shown before. The last digest went out "
                  f"{since.strftime('%a %d %b at %H:%M UTC')}.", ""]

    if total == 0:
        lines += ["Nothing qualified this time.", "",
                  f"  {counts['qualifying']} met your criteria, "
                  f"{counts['already_sent']} were in an earlier digest, "
                  f"{counts['below_band']} did not meet the criteria, "
                  f"{counts.get('outside_configured_locations', 0)} were outside your"
                  " configured locations.",
                  "", "Nothing has been sent on your behalf and nothing has been"
                  " applied for."]
        return subject, "\n".join(lines)

    for group in data["groups"]:
        lines += [group["heading"].upper(), "-" * len(group["heading"]), ""]
        for row in group["rows"]:
            job = row["job"]
            row["fresh_hours"] = data["fresh_hours"]
            lines.append(f"  {_clean(job.get('title'), 120) or 'Title not recorded'}")
            lines.append(f"    {_clean(job.get('company'), 100) or 'Company not recorded'}"
                         f"  |  {_clean(job.get('location'), 80) or 'location not stated'}")
            salary = _salary(job)
            lines.append(f"    Salary: {salary}" if salary else "    Salary: not published")
            lines.append(f"    {_age(job, now)}")
            lines.append(f"    Why: {_why(row)}")
            lines.append(f"    {_clean(job.get('url'), URL_LIMIT) or 'no link recorded'}")
            lines.append("")
        lines.append("")

    if counts["held_back"]:
        lines += [f"{counts['held_back']} further role{'s' if counts['held_back'] != 1 else ''}"
                  f" also qualified and were held back by the {data['limit']}-a-day cap."
                  " They will appear in the next digest.", ""]

    lines += ["--",
              "Selected by fixed rules from vacancies already collected: your recorded"
              " evidence, the employer's own posting date where published, and whether"
              " the title is one your configured searches would have found.",
              "No prediction is made about whether any employer will respond.",
              "Nothing has been applied for and no message has been sent to any employer"
              " or recruiter."]
    return subject, "\n".join(lines)


def build_message(data: dict, recipient: str) -> EmailMessage:
    """A digest to the reader's own mailbox. No attachment, no tracking, no HTML."""
    if not recipient:
        raise ValueError("A digest recipient is required")
    subject, body = render(data)
    message = EmailMessage()
    message["To"] = recipient
    message["From"] = recipient
    message["Subject"] = subject
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(body)
    return message
