"""An offline demo of the digest on a store of invented vacancies. Sends nothing.

    python scripts/demo.py                  # seed a throwaway store, print the digest
    python scripts/demo.py --data PATH      # keep the synthetic store at a new PATH

Every employer, role and link is invented (links use the reserved .invalid domain).
It runs the same path the 07:00 schedule runs, `digest_cli`, with --dry-run, so
selection, the three signals, sectioning and rendering are the real code. Gmail is
never contacted: a dry run does not build a mail client. Needs only the standard
library.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

BANNER = ("SYNTHETIC DEMO - every employer, role and link below is invented. "
          "Nothing is sent and Gmail is not contacted.")

SETTINGS = {
    "locations": {"GB": {"enabled": True, "cities": ["London"]},
                  "FR": {"enabled": True, "cities": ["Paris"]}},
    # The searches the reader has configured. A role that fits but whose title is
    # not made of these words earns the "adjacent" signal.
    "search": {"web": {"queries": ["data engineer London", "python developer Paris"]}},
    # The dry run still resolves a recipient. No real address is involved.
    # allow_other_recipient: the synthetic address is not the owner's mailbox, and a dry
    # run sends nothing, so the documented switch is set rather than bypassing the guard.
    "digest": {"to": "reader@example.invalid", "limit": 25, "allow_other_recipient": True},
}


def _jobs(now: datetime) -> list[tuple[dict, str]]:
    def ago(**delta):
        return (now - timedelta(**delta)).isoformat()
    fits = {"candidacy": {"fit_band": "plausible"}}
    return [
        ({"title": "Data Engineer", "company": "Example Grid Ltd (synthetic)",
          "location": "London, United Kingdom", "posted_at": ago(hours=3), "evaluation": fits,
          "salary_currency": "GBP", "salary_min": 55000, "salary_max": 65000, "salary_period": "annual"},
         "greenhouse"),
        ({"title": "Carbon Accounting Analyst", "company": "Sample Ledger Co (synthetic)",
          "location": "London", "posted_at": ago(hours=30), "evaluation": fits}, "lever"),
        ({"title": "Operations Research Analyst", "company": "Placeholder Freight (synthetic)",
          "location": "London", "posted_at": None, "evaluation": fits,
          # A parse artefact the digest must report as unpublished, not print.
          "salary_currency": "unknown", "salary_min": 20, "salary_max": 20, "salary_period": "annual"},
         "ashby"),
        ({"title": "Python Developer", "company": "Demo Orbit SAS (synthetic)",
          "location": "Paris, France", "posted_at": None,
          "evaluation": {"candidacy": {"fit_band": "strong"}}}, "ashby"),
        ({"title": "Platform Engineer", "company": "Fictional Kiln Studio (synthetic)",
          "location": "Paris, France", "posted_at": ago(days=10), "evaluation": fits}, "adzuna"),
        # Excluded: outside every configured location.
        ({"title": "Data Engineer", "company": "Invented Harbour Co (synthetic)",
          "location": "Shanghai, China", "country": "CN", "posted_at": ago(hours=2),
          "evaluation": fits}, "greenhouse"),
        # Excluded: the recorded evidence does not support it.
        ({"title": "Veterinary Surgeon", "company": "Example Animal Care (synthetic)",
          "location": "London", "posted_at": ago(hours=1),
          "evaluation": {"candidacy": {"fit_band": "not_suitable"}}}, "lever"),
    ]


def seed(path: Path, now: datetime) -> None:
    from careerops.store import Store
    if path.exists():
        # Never overwrite a store that is already there: --data could point at a real one.
        raise SystemExit(f"{path} already exists; choose a new path for the synthetic store.")
    store = Store(str(path))
    store.put_meta("settings", SETTINGS)
    with store.connect() as db:
        for index, (job, source) in enumerate(_jobs(now), start=1):
            record = {**job, "url": f"https://jobs.example.invalid/{index}",
                      "first_seen": (now - timedelta(hours=1)).isoformat(), "synthetic": True}
            db.execute("INSERT INTO jobs (id,identity,fingerprint,data) VALUES (?,?,?,?)",
                       (index, f"synthetic-{index}", f"synthetic-fp-{index}", json.dumps(record)))
            db.execute("INSERT INTO sources (job_id,identity,data) VALUES (?,?,?)",
                       (index, f"synthetic-src-{index}", json.dumps({"type": source})))
        for kind in ("greenhouse", "lever", "ashby"):
            db.execute("INSERT INTO employer_boards VALUES (?,?)",
                       (f"https://{kind}.example.invalid/synthetic",
                        json.dumps({"type": kind, "synthetic": True})))
        db.commit()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, default=None, help="Keep the synthetic store here.")
    args = parser.parse_args(argv)

    from careerops import digest_cli
    import digest_ingestion_census

    with tempfile.TemporaryDirectory() as scratch:
        path = args.data or Path(scratch) / "synthetic.sqlite3"
        seed(path, datetime.now(timezone.utc))
        print(BANNER, end="\n\n")
        print("What was collected (scripts/digest_ingestion_census.py):\n")
        digest_ingestion_census.main([str(path)])
        print("What the 07:00 digest would say (careerops.digest_cli --dry-run):\n")
        code = digest_cli.main(["--dry-run", "--data", str(path)])
        print("\n" + BANNER)
        return code


if __name__ == "__main__":
    sys.exit(main())
