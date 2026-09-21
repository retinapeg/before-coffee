"""Count what discovery has collected, so a change can be proved not to have narrowed it.

The digest branch changes what is EMAILED. It must not change what is COLLECTED.
That claim is only worth something if it is measured the same way before and after,
by a script in the repository rather than by a query someone typed once.

Usage:
    python scripts/digest_ingestion_census.py <store.sqlite3> [--save path.json]
    python scripts/digest_ingestion_census.py <store.sqlite3> --compare baseline.json

`--compare` exits non-zero if ANY count fell. Nothing here writes to the store.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys


def census(path: str) -> dict:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    jobs_by_source_type: dict[str, int] = {}
    for row in db.execute("SELECT data FROM sources"):
        try:
            kind = (json.loads(row["data"]) or {}).get("type") or "unrecorded"
        except (TypeError, ValueError):
            kind = "unparseable"
        jobs_by_source_type[kind] = jobs_by_source_type.get(kind, 0) + 1

    # The table is `employer_boards`, not `boards` - reading the wrong name silently
    # reports zero boards, which would hide exactly the regression this guards against.
    boards_by_type: dict[str, int] = {}
    board_rows = (list(db.execute("SELECT data FROM employer_boards"))
                  if _has_table(db, "employer_boards") else [])
    if not board_rows:
        raise SystemExit("employer_boards is empty or missing - refusing to record a "
                         "baseline that would hide a board regression")
    for row in board_rows:
        try:
            kind = (json.loads(row["data"]) or {}).get("type") or "unrecorded"
        except (TypeError, ValueError):
            kind = "unparseable"
        boards_by_type[kind] = boards_by_type.get(kind, 0) + 1

    # fingerprint is a COLUMN on jobs, not a key inside the job JSON. Reading it from
    # the JSON returns nothing and looks like a catastrophic dedup regression.
    columns = {r["name"] for r in db.execute("PRAGMA table_info(jobs)")}
    totals = {
        "jobs": db.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"],
        "source_rows": db.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"],
        "employer_boards": len(board_rows),
    }
    if "fingerprint" in columns:
        totals["distinct_fingerprints"] = db.execute(
            "SELECT COUNT(DISTINCT fingerprint) c FROM jobs").fetchone()["c"]
    db.close()
    return {"jobs_by_source_type": jobs_by_source_type, "boards_by_type": boards_by_type,
            "totals": totals}


def _has_table(db, name: str) -> bool:
    return bool(db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def compare(baseline: dict, current: dict) -> list[str]:
    """Every count that fell. Empty means ingestion did not narrow."""
    fell = []
    for section in ("jobs_by_source_type", "boards_by_type", "totals"):
        was, now = baseline.get(section, {}), current.get(section, {})
        for key in sorted(set(was) | set(now)):
            before, after = int(was.get(key, 0) or 0), int(now.get(key, 0) or 0)
            if after < before:
                fell.append(f"{section}/{key}: {before} -> {after}")
    return fell


def _render(baseline: dict | None, current: dict) -> str:
    lines = []
    for section in ("jobs_by_source_type", "boards_by_type", "totals"):
        lines.append(section.upper().replace("_", " "))
        was, now = (baseline or {}).get(section, {}), current.get(section, {})
        for key in sorted(set(was) | set(now)):
            before, after = int(was.get(key, 0) or 0), int(now.get(key, 0) or 0)
            delta = f"  {after - before:+d}" if baseline else ""
            shown = f"{before:>7} -> " if baseline else ""
            lines.append(f"  {key:<20} {shown}{after:>7}{delta}")
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("store")
    parser.add_argument("--save")
    parser.add_argument("--compare")
    args = parser.parse_args(argv)

    current = census(args.store)
    baseline = json.load(open(args.compare)) if args.compare else None
    print(_render(baseline, current))

    if args.save:
        with open(args.save, "w", encoding="utf-8") as handle:
            json.dump(current, handle, indent=2, sort_keys=True)
            handle.write("\n")
        print(f"saved {args.save}")

    if baseline is not None:
        fell = compare(baseline, current)
        if fell:
            print("INGESTION NARROWED:")
            for line in fell:
                print("  " + line)
            return 1
        print("No count fell. Ingestion breadth preserved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
