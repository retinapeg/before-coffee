"""Send the morning digest. The entry point a schedule calls.

    python -m careerops.digest_cli --dry-run          # print it, send nothing
    python -m careerops.digest_cli --send             # send it
    python -m careerops.digest_cli --send --resend    # ignore the ledger (testing)

Deliberately separate from the web app. The app serves pages and collects; this sends
one email to its owner and exits. Nothing else in the project gains the ability to
send anything by this module existing, and no legacy submission, email or browser
worker is started or imported.

--save writes the rendered digest to a file BEFORE attempting delivery. Composing the
email needs no credentials, so a mail failure should not also destroy the morning's
content: the file is readable whatever Gmail does. It is written to gitignored local
data and the ledger is not touched, because nothing has been shown to anyone yet.

Exit codes: 0 sent or nothing qualified, 2 a delivery problem the owner must act on,
3 bad arguments. A schedule can therefore alert on 2 without treating a quiet morning
as a failure.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Send the CareerOps job digest.")
    parser.add_argument("--data", type=Path, default=Path("local_data/careerops.sqlite3"))
    parser.add_argument("--send", action="store_true", help="Actually send it.")
    parser.add_argument("--dry-run", action="store_true", help="Print it and send nothing.")
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of roles.")
    parser.add_argument("--fresh-hours", type=int, default=None)
    parser.add_argument("--resend", action="store_true",
                        help="Include roles an earlier digest already listed.")
    parser.add_argument("--save", type=Path, default=None,
                        help="Directory to write the rendered digest into before sending.")
    args = parser.parse_args(argv)

    if args.send == args.dry_run:
        parser.error("Choose exactly one of --send or --dry-run.")
    if not args.data.exists():
        print(f"No store at {args.data}", file=sys.stderr)
        return 3

    from careerops.store import Store
    from careerops import digest, digest_delivery

    store = Store(str(args.data))
    data = digest.select(store, limit=args.limit, fresh_hours=args.fresh_hours,
                         include_already_sent=args.resend)
    counts = data["counts"]
    print("considered %(considered)d | qualifying %(qualifying)d | selected %(selected)d | "
          "held back %(held_back)d | in an earlier digest %(already_sent)d | "
          "below the criteria %(below_band)d | "
          "outside configured locations %(outside_configured_locations)d" % counts)

    if args.save:
        subject, body = digest.render(data)
        stamp = data["now"].replace(":", "").replace("-", "")[:15]
        args.save.mkdir(parents=True, exist_ok=True, mode=0o700)
        written = args.save / f"digest-{stamp}.txt"
        written.write_text(f"Subject: {subject}\n\n{body}\n", encoding="utf-8")
        written.chmod(0o600)
        print(f"Written to {written}")

    try:
        result = digest_delivery.deliver(store, data, dry_run=args.dry_run)
    except digest_delivery.DeliveryError as error:
        print(f"Digest not sent: {error}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"\n--- would send to {result['recipient']} ---")
        print(f"Subject: {result['subject']}\n")
        print(result.get("body", ""))
        return 0

    if result["sent"]:
        print(f"Sent {result['jobs']} roles to {result['recipient']} "
              f"(Gmail id {result['message_id']}); "
              f"{result['total_ever_sent']} roles recorded as sent in total.")
    else:
        print(f"Nothing sent: {result['reason']}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
