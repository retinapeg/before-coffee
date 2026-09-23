"""The selection layer and the poll: what gets chosen, and what they cannot touch.

The dominant constraint is that ingestion breadth and digest selectivity are separate.
The first two tests make that structural rather than a promise: the signal functions
take a job and settings and return marks, so there is no path by which they could
remove a job from the database even if someone wanted one. The poll tests cover the
scheduler that keeps inventory arriving overnight.

Both modules are carried over from careerops/fresh-jobs-20260921 rather than rewritten.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from careerops import refresh, signals

NOW = datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc)
SETTINGS = {"strategy": {}, "locations": {}}

def job(**overrides):
    base = {"id": 1, "title": "Quantitative Developer", "company": "Example",
            "posted_at": (NOW - timedelta(hours=3)).isoformat(),
            "evaluation": {"candidacy": {"fit_band": "plausible"}}}
    return {**base, **overrides}


# --- the separation of concerns, asserted structurally ----------------------------

def test_evaluating_signals_cannot_change_what_is_stored():
    """`evaluate` is a pure read over one job. It takes no store, no connection and
    no writer, so no configuration of it can narrow ingestion."""
    import inspect
    parameters = set(inspect.signature(signals.evaluate).parameters)
    assert parameters == {"job", "settings", "now", "fresh_hours", "source_type", "searched"}
    before = job()
    snapshot = dict(before)
    signals.evaluate(before, SETTINGS, now=NOW, searched=set())
    assert before == snapshot, "evaluating a job must not mutate it"


def test_an_aggregator_job_is_still_evaluated_and_returned_just_not_flagged():
    """Aggregator vacancies stay in the database and stay on the page. They simply
    do not earn the SOURCE signal."""
    marks = signals.evaluate(job(), SETTINGS, now=NOW, source_type="linkedin", searched=set())
    assert marks["source"]["hit"] is False and marks["source"]["kind"] == "aggregator"
    assert marks["adjacent"]["hit"] is True, "it is still assessed on every other signal"


# --- SOURCE ------------------------------------------------------------------------

@pytest.mark.parametrize("source_type", sorted(signals.ATS_SOURCES))
def test_employer_boards_earn_the_source_signal(source_type):
    assert signals.evaluate(job(), SETTINGS, now=NOW, source_type=source_type,
                            searched=set())["source"]["hit"] is True


def test_an_unrecorded_source_is_neither_ats_nor_aggregator():
    marks = signals.evaluate(job(), SETTINGS, now=NOW, source_type=None, searched=set())
    assert marks["source"]["kind"] == "unknown" and marks["source"]["hit"] is False


# --- FRESH: the signal the project rules constrain most tightly ---------------------------

def test_a_job_with_no_posting_date_is_unknown_age_and_never_fresh():
    marks = signals.evaluate(job(posted_at=None), SETTINGS, now=NOW, searched=set())
    assert marks["fresh"]["hit"] is False and marks["fresh"]["age_hours"] is None


def test_first_seen_is_never_used_as_a_posting_date():
    """Project rule: "First seen is never a posting date." A job discovered seconds ago
    with no published posting date must not become fresh."""
    recent = job(posted_at=None, first_seen=NOW.isoformat(), discovered_at=NOW.isoformat())
    assert signals.evaluate(recent, SETTINGS, now=NOW, searched=set())["fresh"]["hit"] is False


@pytest.mark.parametrize("hours,expected", [(0.5, True), (23, True), (24, True), (25, False), (200, False)])
def test_the_freshness_window_is_the_configured_one(hours, expected):
    stale = job(posted_at=(NOW - timedelta(hours=hours)).isoformat())
    assert signals.evaluate(stale, SETTINGS, now=NOW, fresh_hours=24,
                            searched=set())["fresh"]["hit"] is expected


def test_a_posting_date_in_the_future_is_not_fresh():
    """A feed reporting tomorrow is wrong, not new."""
    ahead = job(posted_at=(NOW + timedelta(hours=5)).isoformat())
    assert signals.evaluate(ahead, SETTINGS, now=NOW, searched=set())["fresh"]["hit"] is False


# --- ADJACENT ----------------------------------------------------------------------

def test_a_title_made_of_terms_you_already_search_for_is_not_adjacent():
    marks = signals.evaluate(job(title="Python Engineer"), SETTINGS, now=NOW,
                             searched={"python", "engineer"})
    assert marks["adjacent"]["hit"] is False
    assert "python" in marks["adjacent"]["why"]


def test_a_title_outside_your_searches_that_still_fits_is_adjacent():
    marks = signals.evaluate(job(title="Solutions Consultant"), SETTINGS, now=NOW,
                             searched={"python", "quantitative"})
    assert marks["adjacent"]["hit"] is True


def test_a_role_the_evidence_does_not_support_is_not_adjacent_however_unusual():
    """Adjacent means "fits but unsearched", not merely "unsearched"."""
    poor = job(title="Veterinary Surgeon", evaluation={"candidacy": {"fit_band": "not_suitable"}})
    assert signals.evaluate(poor, SETTINGS, now=NOW, searched=set())["adjacent"]["hit"] is False


def test_seniority_words_do_not_make_a_title_look_unfamiliar():
    """Without stripping noise, "Senior Python Engineer" would read as adjacent to a
    search for "Python Engineer", and every role would look novel."""
    marks = signals.evaluate(job(title="Senior Python Engineer (Remote, UK)"), SETTINGS,
                             now=NOW, searched={"python", "engineer"})
    assert marks["adjacent"]["hit"] is False


# --- the alert is the intersection --------------------------------------------------

def test_an_alert_needs_all_three_signals():
    full = signals.evaluate(job(title="Solutions Consultant"), SETTINGS, now=NOW,
                            source_type="greenhouse", searched={"python"})
    assert full["alert"] is True and full["count"] == 3
    for missing in ({"source_type": "linkedin"}, {"searched": {"solutions", "consultant"}}):
        partial = signals.evaluate(job(title="Solutions Consultant"), SETTINGS, now=NOW,
                                   **{"source_type": "greenhouse", "searched": {"python"}, **missing})
        assert partial["alert"] is False


def test_summarise_counts_each_signal_independently():
    rows = [{"signals": signals.evaluate(job(title=t), SETTINGS, now=NOW,
                                         source_type=s, searched={"python"})}
            for t, s in (("Solutions Consultant", "greenhouse"), ("Python Engineer", "linkedin"))]
    summary = signals.summarise(rows)
    assert summary == {"total": 2, "alerting": 1, "source": 1, "fresh": 2, "adjacent": 1, "unknown_age": 0}


# --- the scheduler ------------------------------------------------------------------

def test_the_background_poll_is_off_until_deliberately_enabled(tmp_path):
    from careerops.store import Store
    store = Store(str(tmp_path / "s.sqlite3"))
    assert refresh.settings(store)["enabled"] is False
    assert refresh.due(store) is False


def test_intervals_are_clamped_to_the_supported_range(tmp_path):
    from careerops.store import Store
    store = Store(str(tmp_path / "s.sqlite3"))
    for configured, expected in ((0, 1), (1, 1), (999, 168), ("nonsense", 1), (None, 1)):
        settings = store.settings()
        settings["refresh"] = {"enabled": True, "interval_hours": configured}
        store.put_meta("settings", settings)
        assert refresh.settings(store)["interval_hours"] == expected


def test_scope_alternates_and_survives_a_restart(tmp_path):
    """Persisted, not held in a thread: an app opened briefly each day must not
    always start at london and leave overseas permanently behind."""
    from careerops.store import Store
    store = Store(str(tmp_path / "s.sqlite3"))
    settings = store.settings()
    settings["refresh"] = {"enabled": True, "interval_hours": 1}
    store.put_meta("settings", settings)
    refresh.schedule_next(store, scope_just_run="london", now=NOW)
    assert refresh.state(store)["next_scope"] == "overseas"
    reopened = Store(str(tmp_path / "s.sqlite3"))
    assert refresh.state(reopened)["next_scope"] == "overseas"


def test_a_poll_is_due_immediately_on_first_enable_then_waits(tmp_path):
    from careerops.store import Store
    store = Store(str(tmp_path / "s.sqlite3"))
    settings = store.settings()
    settings["refresh"] = {"enabled": True, "interval_hours": 1}
    store.put_meta("settings", settings)
    assert refresh.due(store, now=NOW) is True
    refresh.schedule_next(store, scope_just_run="london", now=NOW)
    assert refresh.due(store, now=NOW) is False
    assert refresh.due(store, now=NOW + timedelta(hours=1, seconds=1)) is True


def test_a_manual_search_is_never_interrupted_by_the_poll(tmp_path):
    """The poll skips its turn rather than cancelling or queueing behind a run the
    user started."""
    from careerops.store import Store

    class FakeApp:
        def __init__(self, store):
            self.store, self.running_ids = store, {7}
        def start_search(self, *a, **k):
            raise AssertionError("the poll must not start a search while one is running")

    store = Store(str(tmp_path / "s.sqlite3"))
    settings = store.settings()
    settings["refresh"] = {"enabled": True, "interval_hours": 1}
    store.put_meta("settings", settings)
    assert refresh.poll_once(FakeApp(store))["reason"] == "search_already_running"

def _scheduling_store(tmp_path, **overrides):
    from careerops.store import Store
    store = Store(str(tmp_path / "s.sqlite3"))
    settings = store.settings()
    settings["refresh"] = {"enabled": True, "interval_hours": 1, **overrides}
    store.put_meta("settings", settings)
    return store


def test_a_scheduled_run_can_only_widen_coverage_never_narrow(tmp_path):
    """The dominant constraint: nothing this branch does may reduce what is collected.

    A configured limit that is already higher than the scheduled floor must survive
    untouched, so enabling the schedule cannot quietly shrink a manual search.
    """
    store = _scheduling_store(tmp_path)
    settings = store.settings()
    settings.setdefault("search", {})["scope_budgets"] = {
        "london": {"deep": {"max_board_requests": 9999, "timeout_seconds": 5000,
                            "per_host_limit": 4242}}}
    store.put_meta("settings", settings)

    merged = refresh.scheduled_budget(store, "london", consumed_seconds=0.0)
    assert merged["max_board_requests"] == 9999, "a higher configured limit was lowered"
    assert merged["timeout_seconds"] == 5000, "a longer configured timeout was shortened"
    assert merged["per_host_limit"] == 4242
    from careerops.discovery import COVERAGE_DEFAULTS
    for key, floor in COVERAGE_DEFAULTS.items():
        assert merged[key] >= floor, f"{key} fell below the project default"


def test_a_first_scheduled_run_on_a_fresh_store_is_never_below_the_project_defaults(tmp_path):
    """With nothing configured in scope_budgets, the slot this writes is the highest
    precedence one, so a value below COVERAGE_DEFAULTS would narrow every scheduled run.
    The slice (600s) is shorter than the default timeout (900s)."""
    from careerops.discovery import COVERAGE_DEFAULTS
    store = _scheduling_store(tmp_path, slice_seconds=600)
    assert "scope_budgets" not in store.settings().get("search", {})

    merged = refresh.scheduled_budget(store, "london", consumed_seconds=0.0)
    for key, floor in COVERAGE_DEFAULTS.items():
        assert merged[key] >= floor, f"{key} fell below the project default"
    stored = store.settings()["search"]["scope_budgets"]["london"]["deep"]
    assert stored == merged


def test_the_budget_grows_along_the_chain_or_a_resumed_run_does_no_work(tmp_path):
    """alive() charges elapsed_before against timeout_seconds, so a continued run whose
    budget has not grown stops before its first request."""
    store = _scheduling_store(tmp_path, slice_seconds=600)
    first = refresh.scheduled_budget(store, "london", consumed_seconds=0.0)
    assert first["timeout_seconds"] >= 600
    later = refresh.scheduled_budget(store, "london", consumed_seconds=1800.0)
    assert later["timeout_seconds"] >= 1800 + 600, "a resumed run would get no working time"
    assert later["timeout_seconds"] <= refresh.TIMEOUT_CEILING


def test_a_run_with_queue_left_is_continued_and_a_drained_one_is_not(tmp_path):
    """Restarting instead of continuing is what made the hourly loop re-poll the same
    five boards: a fresh run walks a deterministic registry and dies in the same place."""
    store = _scheduling_store(tmp_path)
    run = store.create_run("deep", None, None)
    store.update_run(run["id"], {"status": "time_limit",
                                 "checkpoint": {"pending": ["a", "b", "c"], "elapsed_seconds": 600}})
    state = refresh.state(store)
    store.put_meta(refresh.STATE_KEY, {**state, "chain": {"london": {"run_id": run["id"], "links": 1}}})

    decision = refresh.continuation(store, "london")
    assert decision["resume"] is True and decision["pending"] == 3
    assert decision["consumed_seconds"] == 600

    store.update_run(run["id"], {"checkpoint": {"pending": [], "elapsed_seconds": 600}})
    drained = refresh.continuation(store, "london")
    assert drained["resume"] is False and drained["reason"] == "queue_drained"


def test_a_completed_run_is_never_resumed(tmp_path):
    store = _scheduling_store(tmp_path)
    run = store.create_run("deep", None, None)
    store.update_run(run["id"], {"status": "completed",
                                 "checkpoint": {"pending": ["a"], "elapsed_seconds": 10}})
    state = refresh.state(store)
    store.put_meta(refresh.STATE_KEY, {**state, "chain": {"london": {"run_id": run["id"], "links": 1}}})
    assert refresh.continuation(store, "london")["reason"] == "not_resumable"


def test_the_chain_is_bounded_so_it_eventually_re_seeds_the_registry(tmp_path):
    """A chain that never ends would never pick up boards verified since it began."""
    store = _scheduling_store(tmp_path, max_chain=3)
    run = store.create_run("deep", None, None)
    store.update_run(run["id"], {"status": "time_limit",
                                 "checkpoint": {"pending": ["a"], "elapsed_seconds": 600}})
    state = refresh.state(store)
    store.put_meta(refresh.STATE_KEY, {**state, "chain": {"london": {"run_id": run["id"], "links": 3}}})
    assert refresh.continuation(store, "london")["reason"] == "chain_length_reached"


def test_a_dead_worker_does_not_block_the_poll_forever(tmp_path):
    """_search_worker clears running_ids in a finally block whose first statement is
    refresh_shortlist. When that raised, the rest of the finally never ran and the id
    stayed forever: every later poll refused with search_already_running and the app
    stopped collecting silently for 55 minutes while due() stayed True."""
    from careerops.store import Store

    class FakeApp:
        def __init__(self, store, running):
            self.store, self.running_ids = store, set(running)
            self.started = []

        def start_search(self, mode, prior_id=None, scope=None):
            self.started.append(scope)
            return {"id": 999}

    store = _scheduling_store(tmp_path)
    dead = store.create_run("deep", None, None)
    store.update_run(dead["id"], {"status": "failed"})
    app = FakeApp(store, {dead["id"]})

    result = refresh.poll_once(app)
    assert result["ran"] is True, "a dead worker blocked the poll"
    assert dead["id"] in result["reaped"]
    assert dead["id"] not in app.running_ids


def test_a_live_search_is_still_never_interrupted(tmp_path):
    """The reaper must not become a way to start a second concurrent search."""
    from careerops.store import Store

    class FakeApp:
        def __init__(self, store, running):
            self.store, self.running_ids = store, set(running)

        def start_search(self, *a, **k):
            raise AssertionError("the poll must not start a search while one is running")

    store = _scheduling_store(tmp_path)
    live = store.create_run("deep", None, None)  # status: running
    app = FakeApp(store, {live["id"]})
    assert refresh.poll_once(app)["reason"] == "search_already_running"
    assert live["id"] in app.running_ids


def test_an_unknown_run_id_is_left_alone(tmp_path):
    """An id with no row might be a run created a microsecond ago. Refusing to poll for
    an hour is a smaller harm than two concurrent searches over one store."""
    from careerops.store import Store

    class FakeApp:
        def __init__(self, store):
            self.store, self.running_ids = store, {424242}

        def start_search(self, *a, **k):
            raise AssertionError("must not start a search against an unknown run id")

    store = _scheduling_store(tmp_path)
    app = FakeApp(store)
    assert refresh.poll_once(app)["reason"] == "search_already_running"
    assert 424242 in app.running_ids
