"""Why a vacancy is worth a second look — computed at read time, never at write time.

This is a RANKING layer. It runs when a page is rendered and decides what gets a
badge and what gets an alert. It is not consulted when a job is discovered, parsed,
deduplicated or stored, so no setting here can reduce what the database collects.
That separation is the whole design: ingestion stays as broad as it has ever been,
and selectivity lives entirely in the presentation.

Three signals, all deterministic and all cheap. No model call.

    SOURCE    the vacancy came from a company's own applicant tracking system
              rather than an aggregator that anyone browsing would already see.
    FRESH     the employer's own posting date is inside the window. Absent is
              NOT fresh, and is never quietly replaced by when we first saw it.
    ADJACENT  it meets the configured criteria while carrying a title the
              configured searches do not look for. This is the one that actually
              delivers "I would not have found this myself".

A job with all three earns an alert. A job with fewer still appears on the page,
badged with whatever it has, because the point is to show why something surfaced
rather than to hide what did not qualify.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

DEFAULT_FRESH_HOURS = 24

# Applicant tracking systems a company runs for itself. A vacancy here is on the
# employer's own board, which is the thing a person browsing aggregators misses.
ATS_SOURCES = {"greenhouse", "lever", "ashby", "workday", "smartrecruiters", "recruitee", "teamtailor"}
# Sources that are somebody else's index. Still collected, still listed, never a
# SOURCE signal, because the premise is that the user would meet these anyway.
AGGREGATOR_SOURCES = {"adzuna", "brave", "indeed", "linkedin", "reed", "totaljobs", "glassdoor", "jsonld"}

# Words that carry no information about what a role IS. Comparing titles without
# removing these makes every "Senior X Engineer" look like every other one.
TITLE_NOISE = {
    "senior", "junior", "lead", "principal", "staff", "graduate", "trainee", "apprentice",
    "associate", "assistant", "head", "chief", "director", "manager", "officer", "specialist",
    "consultant", "executive", "i", "ii", "iii", "iv", "1", "2", "3", "4",
    "uk", "london", "remote", "hybrid", "onsite", "contract", "permanent", "fulltime",
    "full", "time", "part", "and", "or", "of", "the", "for", "with", "in", "at", "to", "a",
    "job", "jobs", "role", "roles", "vacancy", "position", "opportunity", "new", "x", "f", "m", "d",
}
WORD = re.compile(r"[a-z0-9+#]+")


def _words(text) -> set[str]:
    return {w for w in WORD.findall(str(text or "").casefold()) if w not in TITLE_NOISE and len(w) > 1}


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def source_kind(job: dict, source_type: str | None = None) -> str:
    """'ats', 'aggregator' or 'unknown'. Unknown is never treated as either."""
    kind = str(source_type or job.get("source_type") or "").strip().casefold()
    if kind in ATS_SOURCES:
        return "ats"
    if kind in AGGREGATOR_SOURCES:
        return "aggregator"
    return "unknown"


def searched_terms(settings: dict) -> set[str]:
    """Everything the user's own configured searches already look for.

    This is the definition of "would have found it myself": if a title is made of
    words the configured queries already contain, browsing would have surfaced it.
    """
    terms: set[str] = set()
    try:
        from .discovery import _queries
        for entry in _queries(settings, 200):
            query = entry.get("query") if isinstance(entry, dict) else entry
            terms |= _words(query)
    except Exception:
        pass
    for family in (settings.get("strategy", {}) or {}).get("families", []) or []:
        terms |= _words(family)
    return terms


def evaluate(job: dict, settings: dict, *, now: datetime | None = None,
             fresh_hours: int = DEFAULT_FRESH_HOURS, source_type: str | None = None,
             searched: set[str] | None = None) -> dict:
    """The three signals for one job, each with the reason it did or did not fire."""
    now = now or datetime.now(timezone.utc)
    terms = searched_terms(settings) if searched is None else searched

    kind = source_kind(job, source_type)
    source_hit = kind == "ats"

    posted = _parse(job.get("posted_at"))
    age_hours = (now - posted).total_seconds() / 3600 if posted else None
    # Absent posting date is unknown age, never new. AGENTS.md: "First seen is
    # never a posting date." first_seen is deliberately not consulted here at all.
    fresh_hit = age_hours is not None and 0 <= age_hours <= fresh_hours

    title_words = _words(job.get("title"))
    overlap = title_words & terms
    # Adjacent when the evidence says it fits but the title is not something the
    # configured searches would have retrieved.
    #
    # "Meets your criteria" is read from the EVIDENCE band, not from
    # candidacy["recommended"]. Two reasons, one principled and one measured. The
    # principled one: fit_band states whether the candidate's evidence supports the
    # role, while `recommended` is a downstream policy decision about whether to
    # surface it - and this signal is about fit. The measured one: on this branch
    # `recommended` is False for all 1,610 stored jobs, because the recommendation
    # gate closes on any uncertainty and every advert carries some, so reading it
    # would make ADJACENT permanently dead. fit_band varies (204 plausible, 264
    # stretch, 1,142 not_suitable). See SESSION_NOTES.md.
    candidacy = (job.get("evaluation") or {}).get("candidacy") or {}
    meets_criteria = str(candidacy.get("fit_band") or "") in {"strong", "plausible"}
    adjacent_hit = bool(meets_criteria and title_words and not overlap)

    return {
        "source": {
            "hit": source_hit, "kind": kind,
            "why": ("On the employer's own board" if source_hit else
                    "Listed via an aggregator" if kind == "aggregator" else
                    "Source not recorded"),
        },
        "fresh": {
            "hit": fresh_hit,
            "age_hours": round(age_hours, 1) if age_hours is not None else None,
            "why": ("Posted in the last %d hours" % fresh_hours if fresh_hit else
                    "Age unknown — the employer did not publish a posting date" if posted is None else
                    "Posted %.0f days ago" % ((age_hours or 0) / 24)),
        },
        "adjacent": {
            "hit": adjacent_hit,
            "why": ("Meets your criteria, and the title is not one your searches look for"
                    if adjacent_hit else
                    "Title matches terms you already search for: " + ", ".join(sorted(overlap)[:4])
                    if overlap else
                    "Does not currently meet your criteria" if not meets_criteria else
                    "No usable title"),
        },
        # An alert needs all three. Everything else is still listed and still badged.
        "alert": bool(source_hit and fresh_hit and adjacent_hit),
        "count": sum((source_hit, fresh_hit, adjacent_hit)),
    }


def summarise(rows: list[dict]) -> dict:
    """Counts for the page header and the notification."""
    return {
        "total": len(rows),
        "alerting": sum(1 for r in rows if r["signals"]["alert"]),
        "source": sum(1 for r in rows if r["signals"]["source"]["hit"]),
        "fresh": sum(1 for r in rows if r["signals"]["fresh"]["hit"]),
        "adjacent": sum(1 for r in rows if r["signals"]["adjacent"]["hit"]),
        "unknown_age": sum(1 for r in rows if r["signals"]["fresh"]["age_hours"] is None),
    }
