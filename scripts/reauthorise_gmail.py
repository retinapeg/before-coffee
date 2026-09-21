"""Restore Gmail access for the digest. Run this yourself - it opens a browser.

    python scripts/reauthorise_gmail.py --check    # report state, change nothing
    python scripts/reauthorise_gmail.py            # refresh, or re-consent in a browser
    python scripts/reauthorise_gmail.py --send     # ...and send today's digest

Why this exists rather than just calling build_gmail_service(): that function only
reaches its browser-consent branch when there is NO usable token. A token that exists
and has a refresh_token takes the refresh branch instead, which raises RefreshError
and stops. So a dead-but-present token can never re-authorise itself, and the dead
token has to be moved aside first. This does that, safely.

The token path is a symlink into the main checkout, so the BACKUP follows the link and
moves the real file; the consent flow then writes back through the link, keeping one
copy of the secret rather than two.

Nothing here prints a token, a client secret or a refresh token.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


def _paths() -> tuple[Path, Path]:
    from job_cv_agent.gmail_inbox import DEFAULT_TOKEN_PATH, DEFAULT_CREDENTIALS_PATH
    return Path(DEFAULT_TOKEN_PATH), Path(DEFAULT_CREDENTIALS_PATH)


def _describe(token: Path) -> dict:
    import json
    if not token.exists():
        return {"present": False}
    try:
        data = json.loads(token.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return {"present": True, "readable": False, "error": type(error).__name__}
    missing = [s for s in SCOPES if s not in (data.get("scopes") or [])]
    return {"present": True, "readable": True, "expiry": data.get("expiry"),
            "has_refresh_token": bool(data.get("refresh_token")), "missing_scopes": missing}


def _try_refresh(token: Path) -> tuple[bool, str]:
    """Cheapest path. If the refresh token is still alive, no browser is needed."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    try:
        creds = Credentials.from_authorized_user_file(str(token), SCOPES)
    except Exception as error:  # noqa: BLE001
        return False, f"token unreadable ({type(error).__name__})"
    if creds.valid:
        return True, "already valid"
    if not (creds.expired and creds.refresh_token):
        return False, "not refreshable"
    try:
        creds.refresh(Request())
    except Exception as error:  # noqa: BLE001
        return False, f"{type(error).__name__}: {str(error)[:80]}"
    token.write_text(creds.to_json(), encoding="utf-8")
    token.chmod(0o600)
    return True, "refreshed without a browser"


def _consent(token: Path, credentials: Path) -> None:
    """Move the dead token aside and run the browser consent flow."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    if not credentials.exists():
        raise SystemExit(f"No OAuth client at {credentials}. Nothing can be done without it.")

    # resolve() follows the symlink so the REAL file is moved, not the link.
    real = token.resolve()
    if real.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        backup = real.with_name(f"{real.stem}.dead-{stamp}{real.suffix}")
        real.rename(backup)
        print(f"Dead token moved to {backup.name} (kept, not deleted).")

    print("\nOpening your browser. Choose the Google account the digest should come from.")
    print("If you see \"Google hasn't verified this app\", click Advanced then Continue -")
    print("it is your own OAuth client.\n")
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials), SCOPES)
    creds = flow.run_local_server(port=0)
    # Writes back through the symlink, so there is still one copy of the secret.
    token.write_text(creds.to_json(), encoding="utf-8")
    token.chmod(0o600)


def _verify() -> str:
    """Ask Gmail who we are. Proves the credential works end to end."""
    from job_cv_agent.gmail_inbox import build_gmail_service
    service = build_gmail_service()
    profile = service.users().getProfile(userId="me").execute()
    return str((profile or {}).get("emailAddress") or "")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Report state and change nothing.")
    parser.add_argument("--send", action="store_true", help="Send today's digest once authorised.")
    args = parser.parse_args(argv)

    token, credentials = _paths()
    state = _describe(token)
    print(f"Token      : {token}")
    print(f"OAuth client: {'present' if credentials.exists() else 'MISSING'}")
    print(f"State      : {state}")

    if args.check:
        ok, why = _try_refresh(token) if state.get("present") else (False, "no token")
        print(f"\nRefresh check: {'OK - ' if ok else 'would need a browser - '}{why}")
        return 0

    ok, why = (_try_refresh(token) if state.get("present") else (False, "no token"))
    print(f"\nRefresh: {why}")
    if not ok:
        _consent(token, credentials)

    try:
        address = _verify()
    except Exception as error:  # noqa: BLE001
        print(f"\nStill not working: {type(error).__name__}: {str(error)[:160]}", file=sys.stderr)
        print("If this says access_denied, your OAuth consent screen is in Testing and this\n"
              "account is not a listed test user. Add it, or set the app to In production.",
              file=sys.stderr)
        return 2

    print(f"\nAUTHORISED as {address}")
    print("Tokens from a Testing-status consent screen expire after 7 days. Set the app to\n"
          "'In production' in the Google Cloud console to stop this recurring.")

    if args.send:
        print("\nSending today's digest...\n")
        from careerops import digest_cli
        return digest_cli.main(["--send", "--data", str(REPO / "local_data" / "careerops.sqlite3"),
                                "--save", str(REPO / "local_data" / "digests")])
    print(f"\nSend it now with:\n  cd {REPO} && PYTHONPATH=src python -m careerops.digest_cli "
          f"--send --save local_data/digests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
