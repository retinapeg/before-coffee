"""The digest: what it selects, what it says, and where it is allowed to send.

Every job here is invented. Example-based tests over the owner's own store would
quietly become tests about the owner, would leak their data into the repository, and
would break whenever discovery ran. The properties asserted are the ones the project's
rules actually require, so they hold for any store.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from careerops import digest, digest_delivery
from careerops.store import Store

NOW = datetime(2026, 9, 21, 7, 0, tzinfo=timezone.utc)


def _store(tmp_path, jobs, *, settings=None):
    store = Store(str(tmp_path / "digest.sqlite3"))
    configured = store.settings()
    configured.setdefault("locations", {})["GB"] = {"enabled": True, "cities": ["London"]}
    configured["digest"] = {"to": "owner@example.invalid", "limit": 25,
                            # Geography is exercised by its own test; the rest of the
                            # suite is about selection, so it opts out explicitly.
                            "require_configured_location": False,
                            **(settings or {})}
    store.put_meta("settings", configured)
    with store.connect() as db:
        for index, job in enumerate(jobs, start=1):
            record = {"title": "Role %d" % index, "company": "Company %d" % index,
                      "location": "London", "url": "https://example.invalid/%d" % index,
                      "evaluation": {"candidacy": {"fit_band": "plausible"}},
                      "first_seen": (NOW - timedelta(hours=2)).isoformat(), **job}
            db.execute("INSERT INTO jobs (id,identity,fingerprint,data) VALUES (?,?,?,?)",
                       (index, "id-%d" % index, "fp-%d" % index, json.dumps(record)))
            db.execute("INSERT INTO sources (job_id,identity,data) VALUES (?,?,?)",
                       (index, "src-%d" % index,
                        json.dumps({"type": job.get("_source", "greenhouse")})))
        db.commit()
    return store


def _body(store, **kwargs):
    data = digest.select(store, now=NOW, **kwargs)
    return data, digest.render(data)[1]


# --- what counts as new, and what counts as old -------------------------------------

def test_a_job_with_no_posting_date_is_listed_and_labelled_not_guessed(tmp_path):
    """Project rule: first seen is never a posting date. A job the employer dated is the
    only kind that can be called recent."""
    store = _store(tmp_path, [{"posted_at": None, "first_seen": NOW.isoformat()}])
    data, body = _body(store)
    assert data["counts"]["selected"] == 1, "a job with no posting date must still be listed"
    assert "posting date not published" in body
    assert "posted within the hour" not in body, "first_seen was used as an age"


def test_first_seen_being_recent_never_makes_a_job_look_freshly_posted(tmp_path):
    store = _store(tmp_path, [{"posted_at": (NOW - timedelta(days=200)).isoformat(),
                               "first_seen": NOW.isoformat()}])
    _, body = _body(store)
    assert "200 days ago" in body


def test_the_age_shown_comes_from_the_employers_date(tmp_path):
    store = _store(tmp_path, [{"posted_at": (NOW - timedelta(hours=5)).isoformat()}])
    _, body = _body(store)
    assert "posted 5 hours ago" in body


# --- the cap, the grouping and the ledger --------------------------------------------

def test_the_cap_holds_and_the_remainder_is_reported_not_dropped_silently(tmp_path):
    store = _store(tmp_path, [{} for _ in range(40)], settings={"limit": 25})
    data, body = _body(store)
    assert data["counts"]["selected"] == 25
    assert data["counts"]["held_back"] == 15
    assert "15 further roles" in body, "a silent truncation reads as complete coverage"


def test_every_selected_job_appears_under_exactly_one_heading(tmp_path):
    """A job qualifies for several reasons at once; printing it three times would make
    a 25-role email feel like 60."""
    store = _store(tmp_path, [{"posted_at": (NOW - timedelta(hours=1)).isoformat(),
                               "title": "Unheard Of Discipline"} for _ in range(6)])
    data, body = _body(store)
    listed = sum(len(group["rows"]) for group in data["groups"])
    assert listed == data["counts"]["selected"]
    for row in data["rows"]:
        assert body.count(row["job"]["url"]) == 1, "a job was listed more than once"


def test_a_job_already_sent_is_not_sent_again(tmp_path):
    store = _store(tmp_path, [{} for _ in range(3)])
    first = digest.select(store, now=NOW)
    assert first["counts"]["selected"] == 3
    digest.record_sent(store, [row["job"]["id"] for row in first["rows"]], now=NOW)
    again = digest.select(store, now=NOW + timedelta(days=1))
    assert again["counts"]["selected"] == 0, "the ledger did not prevent a repeat"
    assert again["counts"]["already_sent"] == 3


def test_a_role_held_back_by_the_cap_arrives_in_the_next_digest(tmp_path):
    """The email promises exactly this. A "since the last digest" time floor would
    break the promise, because every held-back role was first seen before the digest
    that could not fit it."""
    store = _store(tmp_path, [{} for _ in range(30)], settings={"limit": 25})
    first = digest.select(store, now=NOW)
    assert first["counts"]["selected"] == 25 and first["counts"]["held_back"] == 5
    digest.record_sent(store, [row["job"]["id"] for row in first["rows"]], now=NOW)

    # A day later, with nothing newly collected at all.
    second = digest.select(store, now=NOW + timedelta(days=1))
    assert second["counts"]["selected"] == 5, "the held-back roles were lost"
    already = {row["job"]["id"] for row in first["rows"]}
    assert not any(row["job"]["id"] in already for row in second["rows"])


def test_resending_deliberately_overrides_the_ledger(tmp_path):
    store = _store(tmp_path, [{}])
    data = digest.select(store, now=NOW)
    digest.record_sent(store, [row["job"]["id"] for row in data["rows"]], now=NOW)
    assert digest.select(store, now=NOW, include_already_sent=True)["counts"]["selected"] == 1


# --- what the email may and may not say ----------------------------------------------

# Affirmative claim shapes only. An earlier version of this list matched
# "will respond", which flagged the footer's own denial that it predicts anything -
# a test that forbids the disclaimer is worse than no test.
@pytest.mark.parametrize("forbidden", [
    r"\d+\s?%", r"\bmatch score\b", r"\bodds\b", r"\b\d+/10\b",
    r"\bconfidence: \d", r"\bscore\b\s*[:=]", r"\b(?:strong|good|high) chance\b",
    r"\byou (?:will|should) (?:hear|get|expect)\b", r"\bshortlist(?:ed|ing)? (?:odds|chance)",
])
def test_the_email_never_predicts_or_scores(tmp_path, forbidden):
    """Never a match percentage, never an interview-odds claim."""
    store = _store(tmp_path, [{"posted_at": (NOW - timedelta(hours=2)).isoformat(),
                               "salary_text": "£40,000"} for _ in range(4)])
    _, body = _body(store)
    assert not re.search(forbidden, body, re.I), f"the digest said something matching {forbidden}"


def test_the_email_states_plainly_that_it_predicts_nothing(tmp_path):
    """The other half of the rule: absence of a claim is not the same as saying so."""
    store = _store(tmp_path, [{}])
    _, body = _body(store)
    assert "No prediction is made" in body
    assert "no message has been sent to any employer" in body


def test_an_empty_digest_says_why_and_still_predicts_nothing(tmp_path):
    store = _store(tmp_path, [{"evaluation": {"candidacy": {"fit_band": "not_suitable"}}}])
    data, body = _body(store)
    assert data["counts"]["selected"] == 0
    assert "Nothing qualified" in body
    assert not re.search(r"\d+\s?%", body)


def test_a_hostile_job_title_cannot_break_out_of_the_email(tmp_path):
    """Advert text is untrusted third-party input. It reaches the body, so it must not
    be able to forge headers or inject control characters."""
    store = _store(tmp_path, [{"title": "Engineer\r\nBcc: someone@evil.invalid\r\n",
                               "company": "Ops\x00\x1b[31m"}])
    data = digest.select(store, now=NOW)
    message = digest.build_message(data, "owner@example.invalid")
    assert message["Bcc"] is None, "a title forged a header"
    assert len(message.get_all("To") or []) == 1
    payload = message.get_content()
    assert "\x00" not in payload and "\x1b" not in payload
    assert "someone@evil.invalid" not in (message["To"] or "")


def test_a_salary_parse_artefact_is_reported_as_unpublished(tmp_path):
    """The store holds records with a currency of "unknown" and a 20-to-20 annual
    range. Printed verbatim that became "unknown20 to 20 per annual"."""
    store = _store(tmp_path, [{"salary_currency": "unknown", "salary_min": 20,
                               "salary_max": 20, "salary_period": "annual"}])
    _, body = _body(store)
    assert "Salary: not published" in body
    assert "unknown" not in body.casefold()


def test_a_real_salary_range_is_shown(tmp_path):
    store = _store(tmp_path, [{"salary_currency": "GBP", "salary_min": 45000,
                               "salary_max": 55000, "salary_period": "annual"}])
    _, body = _body(store)
    assert "45,000 to 55,000" in body


# --- geography ------------------------------------------------------------------------

def test_a_role_outside_every_configured_location_is_not_offered(tmp_path):
    """The evidence band says nothing about where a role is, so without this the digest
    offered Shanghai to a London-and-Mediterranean search."""
    store = _store(tmp_path, [{"location": "Shanghai, China", "country": "CN"}],
                   settings={"require_configured_location": True})
    assert digest.select(store, now=NOW)["counts"]["selected"] == 0
    assert digest.select(store, now=NOW)["counts"]["outside_configured_locations"] == 1


def test_the_location_gate_can_be_turned_off(tmp_path):
    store = _store(tmp_path, [{"location": "Shanghai, China", "country": "CN"}],
                   settings={"require_configured_location": False})
    assert digest.select(store, now=NOW)["counts"]["selected"] == 1


# --- where it may send ----------------------------------------------------------------

def test_the_digest_refuses_a_recipient_that_is_not_the_authenticated_mailbox(tmp_path):
    """"This app sends nothing to any third party" has to be a code path, not a promise."""
    store = _store(tmp_path, [{}], settings={"to": "someone.else@example.invalid"})
    with pytest.raises(digest_delivery.DeliveryError, match="authenticated mailbox"):
        digest_delivery.resolve_recipient(store, account="owner@example.invalid")


def test_sending_elsewhere_is_possible_only_when_deliberately_enabled(tmp_path):
    store = _store(tmp_path, [{}], settings={"to": "someone.else@example.invalid",
                                             "allow_other_recipient": True})
    assert digest_delivery.resolve_recipient(
        store, account="owner@example.invalid") == "someone.else@example.invalid"


def test_the_authenticated_mailbox_is_the_default_with_no_address_configured(tmp_path):
    store = _store(tmp_path, [{}], settings={"to": ""})
    assert digest_delivery.resolve_recipient(store, account="owner@example.invalid") == "owner@example.invalid"


def test_an_unusable_token_is_an_error_and_never_a_browser_consent_flow(tmp_path):
    """build_gmail_service falls back to an interactive flow. Unattended at 07:00 that
    would hang forever and would re-authorise without the owner knowing."""
    missing = tmp_path / "absent.json"
    with pytest.raises(digest_delivery.DeliveryError, match="unattended"):
        digest_delivery.gmail_service(path=missing)

    no_refresh = tmp_path / "no_refresh.json"
    no_refresh.write_text(json.dumps({"token": "x", "scopes": list(digest_delivery.REQUIRED_SCOPES)}))
    with pytest.raises(digest_delivery.DeliveryError, match="refresh_token"):
        digest_delivery.gmail_service(path=no_refresh)

    wrong_scope = tmp_path / "wrong_scope.json"
    wrong_scope.write_text(json.dumps({"token": "x", "refresh_token": "y",
                                       "scopes": ["https://www.googleapis.com/auth/gmail.readonly"]}))
    with pytest.raises(digest_delivery.DeliveryError, match="scope"):
        digest_delivery.gmail_service(path=wrong_scope)


def test_nothing_is_sent_when_nothing_qualifies(tmp_path):
    """A "nothing today" email every morning trains the reader to ignore the next one."""
    store = _store(tmp_path, [{"evaluation": {"candidacy": {"fit_band": "not_suitable"}}}])
    sent = []
    data = digest.select(store, now=NOW)
    result = digest_delivery.deliver(store, data, service=object(), now=NOW)
    assert result["sent"] is False and result["reason"] == "nothing_qualified"
    assert sent == []


def test_a_send_records_exactly_what_went_out(tmp_path, monkeypatch):
    store = _store(tmp_path, [{} for _ in range(3)])
    captured = {}

    def fake_send(service, message):
        captured["to"] = message["To"]
        captured["subject"] = message["Subject"]
        captured["body"] = message.get_content()
        return "gmail-id-1"

    monkeypatch.setattr("job_cv_agent.email_delivery.send_job_email", fake_send)
    monkeypatch.setattr(digest_delivery, "mailbox_address", lambda service: "owner@example.invalid")
    data = digest.select(store, now=NOW)
    result = digest_delivery.deliver(store, data, service=object(), now=NOW)

    assert result["sent"] is True and result["jobs"] == 3
    assert captured["to"] == "owner@example.invalid"
    assert digest.ledger(store)["sends"] == 1
    assert len(digest.ledger(store)["job_ids"]) == 3
    assert digest.ledger(store)["last_message_id"] == "gmail-id-1"
    # And the second digest of the same store has nothing left to say.
    assert digest.select(store, now=NOW + timedelta(hours=1))["counts"]["selected"] == 0


def test_the_message_carries_no_attachment_and_no_html(tmp_path):
    store = _store(tmp_path, [{}])
    message = digest.build_message(digest.select(store, now=NOW), "owner@example.invalid")
    assert message.get_content_type() == "text/plain"
    assert not list(message.iter_attachments())
    assert message["Auto-Submitted"] == "auto-generated"


def test_london_and_international_are_separate_sections_in_that_order(tmp_path):
    """Two sections, London then International. Roles outside every configured
    country are still excluded - an International section is not a licence to show
    Shanghai."""
    store = _store(tmp_path, [
        {"location": "London, United Kingdom", "title": "Data Engineer"},
        {"location": "Paris, France", "title": "Data Engineer"},
        {"location": "Shanghai, China", "country": "CN", "title": "Data Engineer"},
    ], settings={"require_configured_location": True})
    configured = store.settings()
    configured["locations"]["FR"] = {"enabled": True, "cities": ["Paris"]}
    store.put_meta("settings", configured)

    data = digest.select(store, now=NOW)
    sections = {g["key"]: [r["job"]["location"] for r in g["rows"]] for g in data["groups"]}
    assert list(sections) == ["london", "international"]
    assert sections["london"] == ["London, United Kingdom"]
    assert sections["international"] == ["Paris, France"]
    assert data["counts"]["outside_configured_locations"] == 1

    body = digest.render(data)[1]
    assert body.index("LONDON\n") < body.index("INTERNATIONAL\n")


def test_the_reason_a_job_qualified_is_still_printed_under_the_new_sections(tmp_path):
    """Moving to location sections must not lose "outside your searches" - it moves
    from a heading to each job's Why line."""
    store = _store(tmp_path, [{"title": "Unheard Of Discipline"}])
    body = digest.render(digest.select(store, now=NOW))[1]
    assert "Why:" in body
