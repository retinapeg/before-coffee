"""A minimal SQLite job store, written as a stand-in for this public extract.

The private system's `Store` is much larger and is not published. This stand-in
keeps only the surface that the digest, the refresh loop, the census script and the
tests use: the five table layouts, JSON records held in a `data` column, settings and
state kept as metadata rows, and discovery run records.

How it differs from the private store:

- No rescore on open. The private `Store.__init__` recalculates and rewrites every
  job's evaluation whenever the store is opened. This one never rewrites a job, so
  `fit_band` is whatever was stored.
- No private defaults. The private `settings()` merges the owner's default policy
  into the stored settings; this one returns a copy of the stored dict only, so a
  fresh store has no locations enabled and no background refresh configured.
- No deduplication, scoring, shortlist or schema migration.
- `create_run` keeps only the fields the refresh loop reads (`status`, `checkpoint`,
  `resumed_from`); the private version also records counters and validates resumes.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY, identity TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, identity TEXT UNIQUE NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS employer_boards (identity TEXT PRIMARY KEY, data TEXT NOT NULL);
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False)


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript(SCHEMA)
            db.execute("INSERT OR IGNORE INTO metadata VALUES (?,?)", ("settings", encode({})))
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    # --- metadata: settings, the digest ledger, the refresh state ----------------

    def meta(self, key, fallback=None):
        with self.connect() as db:
            row = db.execute("SELECT data FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else fallback

    def put_meta(self, key, value, version=False):  # `version` accepted for compatibility
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                       (key, encode(value)))

    def settings(self):
        stored = self.meta("settings", {})
        return copy.deepcopy(stored) if isinstance(stored, dict) else {}

    # --- jobs ----------------------------------------------------------------------

    def jobs(self):
        with self.connect() as db:
            return [dict(json.loads(r["data"]), id=r["id"])
                    for r in db.execute("SELECT id,data FROM jobs ORDER BY id DESC")]

    # --- discovery runs --------------------------------------------------------------

    def runs(self):
        # The private store also returns only the 50 most recent runs.
        with self.connect() as db:
            return [dict(json.loads(r["data"]), id=r["id"])
                    for r in db.execute("SELECT id,data FROM runs ORDER BY id DESC LIMIT 50")]

    def create_run(self, mode, checkpoint=None, prior_id=None):
        """A run is stored as `running` before any worker starts; refresh.py relies on it."""
        value = {"mode": mode, "status": "running", "created_at": now(), "checkpoint": checkpoint or {}}
        if prior_id is not None:
            value["resumed_from"] = prior_id
        with self.lock, self.connect() as db:
            value["id"] = db.execute("INSERT INTO runs(data) VALUES (?)", (encode(value),)).lastrowid
        return value

    def update_run(self, run_id, patch):
        """Shallow-merge `patch` into the stored run; unpatched keys survive."""
        with self.lock, self.connect() as db:
            row = db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError("Run not found")
            value = {**json.loads(row[0]), **patch, "id": int(run_id), "updated_at": now()}
            db.execute("UPDATE runs SET data=? WHERE id=?", (encode(value), run_id))
        return value
