"""A background poll that keeps discovery running while the app is open.

Project policy has been that schedules are off; the owner is turning this one on
deliberately, so it is off by default, gated on an explicit setting, and does
nothing at all until asked.

**It collects. It never filters.** This module opens no new route to discovery: it
calls `Application.start_search`, the same entry point the "Fetch new vacancies"
button uses, so the board registry, the query set, the coverage limits and the
public-only constraint are whatever the manual path already applies. Nothing here
can narrow a source, because nothing here knows what a source is.

Three design points that are not obvious:

**Scope alternates rather than running both.** `start_search` refuses a second
concurrent run, and a single run covers one scope. Alternating london/overseas each
poll covers both over time without ever starving one — the next scope is persisted,
so a restart resumes the alternation instead of always beginning at london and
leaving overseas permanently behind.

**Due time is persisted, not held in memory.** A timer that lives in a thread makes
"every hour" mean "every hour of uptime", so an app opened for twenty minutes a day
would poll once a week. `next_due_at` is stored, so closing the laptop and opening
it tomorrow triggers a poll immediately rather than an hour later.

**A manual search always wins.** If the user has started a search, the poll skips
this cycle and tries again shortly. It never cancels their run and never queues
behind it.

**But a dead search must not win forever.** `_search_worker` clears `running_ids` in a
`finally` block whose FIRST statement is `refresh_shortlist`. When that raised
("database is locked"), the rest of the `finally` never ran and the id stayed in
`running_ids` for the life of the process - so every later poll refused with
`search_already_running` and the app silently stopped collecting. Observed on the
private system: 55 minutes without a poll while `due()` returned True the whole time.
`create_run` writes `status: "running"` before the worker starts, so a run whose
stored status is terminal cannot still be working and its id is stale. The poll reaps
those ids rather than waiting for a restart.

**It notifies nobody.** The only output of this branch is the morning digest, which
runs on its own schedule and reads the store. The loop's job is inventory.

**It continues the previous run instead of restarting it, and this is the whole
reason it collects anything.** Measured on the private system's store (these figures
cannot be reproduced from this repository): a fresh `normal` run dies at `time_limit`
after 180s having contacted 5 of 62 employer boards, and abandons a pending queue of
256 entries. Registry order is deterministic, so runs 15 and 17 -
both london, both fresh - contacted exactly the same eight hosts with identical
per-host counts, and run 17 stored `new_unique: 0`. No Lever host was contacted at
all. An hourly loop that always starts fresh therefore re-polls the same five boards
all night and adds nothing, which is ingestion narrowed by scheduling.

So each poll resumes the previous run's checkpoint, which restores its `pending`
queue, and works through it. `alive()` compares `elapsed_before + time since start`
against `timeout_seconds`, so a resumed run with a budget it has already spent stops
before issuing one request. The budget therefore has to grow along the chain:
`scheduled_budget` sets it to `consumed + slice`, and never below the project default
timeout. Every value it writes is at least `COVERAGE_DEFAULTS` for that key and at
least the value already in the same scope budget slot, so within that slot this can
widen coverage and cannot narrow it. It does not compare against lower-precedence
limits configured elsewhere. When the
pending queue empties the chain is cleared and the next poll starts fresh, which
re-seeds the registry and so picks up boards verified since.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

SETTINGS_KEY = "refresh"
STATE_KEY = "refresh_state"
SCOPES = ("london", "overseas")
MIN_INTERVAL_HOURS = 1
MAX_INTERVAL_HOURS = 168
# How often the loop wakes to ask "is anything due yet". Short enough that a
# restart or a just-finished manual run is noticed promptly, long enough to be
# invisible. This is not the poll interval.
TICK_SECONDS = 30


def defaults() -> dict:
    # deep rather than normal: deep differs from normal only by higher limits and one
    # extra crawl depth, so it is strictly broader and can never collect less.
    return {"enabled": False, "interval_hours": 1, "public_only": True,
            "scopes": list(SCOPES), "mode": "deep", "slice_seconds": 600, "max_chain": 12}


def settings(store) -> dict:
    configured = store.settings().get(SETTINGS_KEY)
    value = {**defaults(), **(configured if isinstance(configured, dict) else {})}
    value["enabled"] = value.get("enabled") is True
    try:
        hours = int(value.get("interval_hours", 1))
    except (TypeError, ValueError):
        hours = 1
    value["interval_hours"] = max(MIN_INTERVAL_HOURS, min(MAX_INTERVAL_HOURS, hours))
    scopes = [s for s in (value.get("scopes") or []) if s in SCOPES]
    value["scopes"] = scopes or list(SCOPES)
    value["public_only"] = value.get("public_only") is not False
    value["mode"] = value.get("mode") if value.get("mode") in {"normal", "deep"} else "deep"
    try:
        value["slice_seconds"] = max(120, min(3600, int(value.get("slice_seconds", 600))))
    except (TypeError, ValueError):
        value["slice_seconds"] = 600
    try:
        value["max_chain"] = max(1, min(48, int(value.get("max_chain", 12))))
    except (TypeError, ValueError):
        value["max_chain"] = 12
    return value


def state(store) -> dict:
    stored = store.meta(STATE_KEY, {})
    stored = stored if isinstance(stored, dict) else {}
    chain = stored.get("chain")
    return {"next_due_at": stored.get("next_due_at"), "next_scope": stored.get("next_scope", SCOPES[0]),
            "last_run_at": stored.get("last_run_at"), "last_run_id": stored.get("last_run_id"),
            "last_error": stored.get("last_error"), "polls": int(stored.get("polls") or 0),
            # The run each scope is continuing, and how many links deep it is.
            "chain": chain if isinstance(chain, dict) else {}}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def due(store, *, now: datetime | None = None) -> bool:
    """Due when enabled and the stored time has passed.

    A missing `next_due_at` means due: a first enable, or a database that predates
    this feature, should poll rather than wait an hour to discover it is working.
    """
    if not settings(store)["enabled"]:
        return False
    stored = _parse(state(store)["next_due_at"])
    return stored is None or (now or _now()) >= stored


def schedule_next(store, *, scope_just_run: str | None = None, now: datetime | None = None,
                  error: str | None = None, run_id=None, chain_for: dict | None = None) -> dict:
    """Persist the next due time and rotate the scope.

    Called after every attempt, including a failure, so a provider being down
    cannot turn into a hot loop.
    """
    configured = settings(store)
    current = state(store)
    scopes = configured["scopes"]
    if scope_just_run in scopes:
        following = scopes[(scopes.index(scope_just_run) + 1) % len(scopes)]
    else:
        following = current["next_scope"] if current["next_scope"] in scopes else scopes[0]
    moment = now or _now()
    updated = {
        "next_due_at": (moment + timedelta(hours=configured["interval_hours"])).isoformat(),
        "next_scope": following,
        "last_run_at": moment.isoformat() if scope_just_run else current["last_run_at"],
        "last_run_id": run_id if run_id is not None else current["last_run_id"],
        "last_error": error,
        "polls": current["polls"] + (1 if scope_just_run else 0),
        "chain": {**current["chain"], **(chain_for or {})},
    }
    store.put_meta(STATE_KEY, updated)
    return updated


# Statuses a run can be continued from. `completed` is absent on purpose: a finished
# run has nothing pending, and start_search refuses to resume one.
RESUMABLE = {"time_limit", "limit_reached", "interrupted", "cancelled", "failed"}
TIMEOUT_CEILING = 7200  # discovery.coverage_limits clamps timeout_seconds to this.


def _run_row(store, run_id):
    if run_id is None:
        return None
    return next((r for r in store.runs() if r.get("id") == run_id), None)


def continuation(store, scope: str, *, configured=None) -> dict:
    """Whether this scope should continue its previous run, and what is left of it.

    A run is worth continuing only if it stopped early AND still has queue entries.
    Anything else starts fresh, which re-seeds the registry from store.boards() and
    so picks up boards verified since the chain began.
    """
    configured = configured or settings(store)
    head = state(store)["chain"].get(scope) or {}
    head = head if isinstance(head, dict) else {"run_id": head, "links": 0}
    links = int(head.get("links") or 0)

    row = _run_row(store, head.get("run_id"))
    if row is None:
        return {"resume": False, "reason": "no_previous_run", "run_id": None, "links": 0}

    checkpoint = row.get("checkpoint") or {}
    pending = checkpoint.get("pending")
    pending_count = len(pending) if isinstance(pending, list) else 0

    if str(row.get("status") or "") not in RESUMABLE:
        return {"resume": False, "reason": "not_resumable", "run_id": row.get("id"),
                "status": row.get("status"), "links": links}
    if pending_count == 0:
        # The queue is drained: the registry has been walked. Start fresh next time.
        return {"resume": False, "reason": "queue_drained", "run_id": row.get("id"), "links": links}
    if links >= configured["max_chain"]:
        return {"resume": False, "reason": "chain_length_reached", "run_id": row.get("id"),
                "links": links, "pending": pending_count}
    return {"resume": True, "reason": "queue_has_work", "run_id": row.get("id"),
            "pending": pending_count, "links": links,
            "consumed_seconds": float(checkpoint.get("elapsed_seconds") or 0)}


def scheduled_budget(store, scope: str, *, consumed_seconds: float, configured=None) -> dict:
    """Widen this scope's coverage budget so a continued run can actually do work.

    The result is written into settings['search']['scope_budgets'][scope][mode], the
    highest-precedence slot in discovery.coverage_limits, and the mode is the
    scheduler's own - so a manual search in another mode is untouched.

    What it guarantees, for every key in COVERAGE_DEFAULTS: the value written is at
    least COVERAGE_DEFAULTS[key], and at least the value already in that same slot.
    timeout_seconds is consumed + slice, capped at TIMEOUT_CEILING, and raised to the
    default timeout when that is smaller. It does not read lower-precedence limits
    configured elsewhere (for example search.limits), so it makes no promise about them.

    Without this a continued run stops before its first request: alive() compares
    elapsed_before plus time-since-start against timeout_seconds, and a resumed run
    starts with elapsed_before already at the old ceiling.
    """
    from .discovery import COVERAGE_DEFAULTS
    configured = configured or settings(store)
    mode = configured["mode"]
    every = store.settings()
    search = every.setdefault("search", {})
    budgets = search.setdefault("scope_budgets", {})
    scoped = budgets.setdefault(scope, {})
    current = dict(scoped.get(mode) or {})

    wanted = int(consumed_seconds) + configured["slice_seconds"]
    # The slice can be shorter than the default timeout, so the default is the floor.
    target = {**{k: COVERAGE_DEFAULTS[k] for k in COVERAGE_DEFAULTS},
              "timeout_seconds": max(COVERAGE_DEFAULTS["timeout_seconds"],
                                     min(TIMEOUT_CEILING, wanted))}
    # max() against the existing value: never narrow what is already in this slot.
    merged = {k: max(int(current.get(k, 0) or 0), int(target[k])) for k in target}
    scoped[mode] = merged
    store.put_meta("settings", every)
    return merged


# A run in one of these states still has a live worker. Anything else is finished,
# however it finished, so its id in running_ids is a leak.
LIVE_STATUSES = {"running", "cancelling"}


def reap_stale_runs(application) -> list:
    """Drop run ids whose worker has died without clearing itself.

    Only ever discards an id whose stored status is POSITIVELY terminal. An id with no
    row, or a row we cannot read, is left alone: refusing to poll for an hour is a far
    smaller harm than starting a second concurrent search over the same store.
    """
    running = getattr(application, "running_ids", None)
    if not running:
        return []
    try:
        statuses = {r.get("id"): str(r.get("status") or "") for r in application.store.runs()}
    except Exception:  # noqa: BLE001 - a failed read must not start a concurrent search
        return []
    stale = [run_id for run_id in list(running)
             if statuses.get(run_id) and statuses[run_id] not in LIVE_STATUSES]
    if not stale:
        return []
    lock = getattr(application, "search_lock", None)
    if lock is not None:
        with lock:
            for run_id in stale:
                running.discard(run_id)
    else:
        for run_id in stale:
            running.discard(run_id)
    return stale


def poll_once(application, *, now: datetime | None = None) -> dict:
    """One cycle. Returns what happened and why, so the UI can explain itself."""
    store = application.store
    configured = settings(store)
    if not configured["enabled"]:
        return {"ran": False, "reason": "disabled"}
    if not due(store, now=now):
        return {"ran": False, "reason": "not_due", "next_due_at": state(store)["next_due_at"]}
    # A manual search owns the one available slot. Skip rather than cancel or queue;
    # the next tick will find us due again in a few seconds. But first clear out any
    # id left behind by a worker that died - see the module docstring.
    reaped = reap_stale_runs(application)
    if application.running_ids:
        return {"ran": False, "reason": "search_already_running", "reaped": reaped}

    scope = state(store)["next_scope"]
    scope = scope if scope in configured["scopes"] else configured["scopes"][0]

    decision = continuation(store, scope, configured=configured)
    budget = scheduled_budget(store, scope, configured=configured,
                              consumed_seconds=decision.get("consumed_seconds", 0.0))
    prior_id = decision["run_id"] if decision["resume"] else None
    try:
        run = application.start_search(configured["mode"], prior_id=prior_id, scope=scope)
    except ValueError as error:
        # start_search raises this when a run appeared between the check and here.
        schedule_next(store, now=now, error=str(error)[:200])
        return {"ran": False, "reason": "refused", "detail": str(error)[:200]}
    except Exception as error:  # noqa: BLE001 - a poll must never kill the loop
        schedule_next(store, now=now, error=f"{type(error).__name__}: {error}"[:200])
        return {"ran": False, "reason": "failed", "detail": type(error).__name__}
    links = (decision.get("links", 0) + 1) if decision["resume"] else 1
    schedule_next(store, scope_just_run=scope, now=now, run_id=run.get("id"),
                  chain_for={scope: {"run_id": run.get("id"), "links": links}})
    return {"ran": True, "scope": scope, "run_id": run.get("id"), "mode": configured["mode"],
            "reaped": reaped, "continued": decision["resume"], "continuation": decision["reason"],
            "pending_before": decision.get("pending"), "chain_links": links,
            "timeout_seconds": budget["timeout_seconds"]}


class RefreshLoop:
    """Owns the polling thread. Started by the server, stopped on shutdown."""

    def __init__(self, application):
        self.application = application
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_result: dict = {"ran": False, "reason": "not_started"}

    def start(self) -> "RefreshLoop":
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="careerops-refresh")
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as error:  # noqa: BLE001
                # The loop outliving a bad cycle matters more than the cycle.
                self.last_result = {"ran": False, "reason": "loop_error", "detail": type(error).__name__}
            self._stop.wait(TICK_SECONDS)

    def tick(self) -> dict:
        """One pass. Separate from the thread so it can be tested a tick at a time."""
        self.last_result = poll_once(self.application)
        return self.last_result

    def status(self) -> dict:
        configured = settings(self.application.store)
        return {**configured, **state(self.application.store), "last_result": self.last_result,
                "running": bool(self._thread and self._thread.is_alive()),
                "note": ("Runs only while the app is open. Discovery is public-only and "
                         "collects vacancies; it never sends anything.")}
