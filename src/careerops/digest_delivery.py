"""Deliver the digest to the reader's own mailbox, and to nowhere else.

AGENTS.md says the app sends no email. That rule is about applications and recruiter
contact, and the owner has decided explicitly that a digest to their own address is
permitted. This module is built so that permission cannot quietly widen:

**The default recipient is the authenticated mailbox itself**, asked of Gmail at send
time rather than configured. Sending anywhere else requires
`digest.allow_other_recipient` to be set to true by hand. So "this app messages no
third party" is enforced by the code path, not by a promise in a document - and the
owner's address never has to be written down in the repository to make it work.

**It cannot start a browser authorisation.** `build_gmail_service` falls back to an
interactive consent flow when a token is unusable, which would hang unattended and
would re-authorise without the owner knowing. Every precondition is therefore checked
first, and a token that cannot be refreshed non-interactively is an error that says
what to do rather than a browser window nobody is sitting in front of.

**Nothing is attached and nothing is tracked.** One plain-text message, no CV, no
pixel, no link shortener, no third-party host.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import digest

REQUIRED_SCOPES = ("https://www.googleapis.com/auth/gmail.send",)


class DeliveryError(RuntimeError):
    """Raised with an action the owner can take, never a bare failure."""


def token_path(root: Path | None = None) -> Path:
    base = root or Path(__file__).resolve().parents[2]
    return base / "local_data" / "google_oauth_token.json"


def credentials_path(root: Path | None = None) -> Path:
    base = root or Path(__file__).resolve().parents[2]
    return base / "local_data" / "google_oauth_credentials.json"


def token_state(path: Path) -> dict:
    """What we can tell about the token WITHOUT contacting Google or printing secrets."""
    if not path.exists():
        return {"usable": False, "reason": "missing", "account": "", "scopes": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return {"usable": False, "reason": f"unreadable ({type(error).__name__})",
                "account": "", "scopes": []}
    scopes = data.get("scopes") or []
    missing = [s for s in REQUIRED_SCOPES if s not in scopes]
    return {
        "usable": bool(data.get("refresh_token")) and not missing,
        "reason": ("no refresh_token - it can only be restored by authorising again"
                   if not data.get("refresh_token") else
                   f"missing scope: {', '.join(missing)}" if missing else "ok"),
        "account": str(data.get("account") or ""),
        "scopes": list(scopes),
        "expiry": data.get("expiry"),
    }


def authenticated_account(path: Path | None = None) -> str:
    return token_state(path or token_path())["account"]


def mailbox_address(service) -> str:
    """Whose mailbox this actually is, according to Gmail.

    Better than any stored value: it is the live answer, it cannot drift from the
    token in use, and it means the owner's address is never committed anywhere to
    make the default work.
    """
    if service is None:
        return ""
    try:
        profile = service.users().getProfile(userId="me").execute()
    except Exception:  # noqa: BLE001 - "" means unknown; deliver() refuses to send on it
        return ""
    return str((profile or {}).get("emailAddress") or "")


def resolve_recipient(store, *, path: Path | None = None, account: str | None = None) -> str:
    """The digest's own mailbox. Anywhere else has to be turned on deliberately."""
    configured = digest.settings(store)
    account = account if account is not None else authenticated_account(path)
    wanted = configured["to"] or account
    if not wanted:
        raise DeliveryError(
            "No recipient. The OAuth token records no account, so set "
            "settings['digest']['to'] to the address the digest should go to.")
    if account and wanted.casefold() != account.casefold() and not configured["allow_other_recipient"]:
        raise DeliveryError(
            "The configured digest recipient is not the authenticated mailbox "
            "Gmail reports. "
            "Sending to another address needs settings['digest']"
            "['allow_other_recipient'] set to true, which is deliberately not the "
            "default: this app sends nothing to any third party.")
    return wanted


def gmail_service(*, path: Path | None = None, creds: Path | None = None):
    """A Gmail client, or a DeliveryError. Never an interactive consent flow."""
    resolved = path or token_path()
    state = token_state(resolved)
    if not state["usable"]:
        raise DeliveryError(
            f"The Google token at {resolved.name} cannot be used unattended: "
            f"{state['reason']}. Authorise again yourself before the digest can send; "
            "nothing here will open a browser consent flow on your behalf.")
    from job_cv_agent.gmail_inbox import build_gmail_service
    try:
        # Reached only with a refresh_token and the send scope present, so
        # build_gmail_service takes its refresh branch and not its flow branch.
        return build_gmail_service(creds or credentials_path(), resolved)
    except Exception as error:  # noqa: BLE001 - surface the action, not a traceback
        raise DeliveryError(
            f"Gmail refused the stored credentials ({type(error).__name__}). "
            "The refresh token may have been revoked; authorise again yourself.") from error


def deliver(store, data: dict, *, service=None, path: Path | None = None,
            now=None, dry_run: bool = False) -> dict:
    """Send one digest and record what went out. Returns what it did and why.

    An empty digest is not sent. A morning email saying "nothing today" trains the
    reader to ignore the next one, and the reason for silence is in the notes.
    """
    subject, body = digest.render(data)
    job_ids = [row["job"].get("id") for row in data["rows"]]

    # Build the client before resolving the recipient: Gmail is the authority on whose
    # mailbox this is, and the self-only guard is checked against its answer.
    client = service
    if client is None and not dry_run:
        client = gmail_service(path=path)
    reported = mailbox_address(client)
    recipient = resolve_recipient(store, path=path, account=reported or None)

    if not job_ids:
        return {"sent": False, "reason": "nothing_qualified", "subject": subject,
                "counts": data["counts"], "recipient": recipient}
    if dry_run:
        return {"sent": False, "reason": "dry_run", "subject": subject, "body": body,
                "recipient": recipient, "jobs": len(job_ids), "counts": data["counts"]}

    # Without Gmail's answer the self-only guard has nothing to compare against: the
    # token file's `account` is often empty, and resolve_recipient then accepts any
    # configured address. So a real send needs the live answer, or the deliberate
    # switch that permits another recipient.
    if not reported and not digest.settings(store)["allow_other_recipient"]:
        raise DeliveryError(
            "Gmail did not report which mailbox this is, so the digest recipient "
            "cannot be checked against it. Nothing was sent. Check the connection and "
            "the token, or set settings['digest']['allow_other_recipient'] to true "
            "if sending to the configured address is intended.")

    message = digest.build_message(data, recipient)
    from job_cv_agent.email_delivery import send_job_email
    try:
        message_id = send_job_email(client, message)
    except Exception as error:  # noqa: BLE001
        raise DeliveryError(f"Gmail did not accept the digest ({type(error).__name__}).") from error

    ledger = digest.record_sent(store, job_ids, now=now, message_id=message_id)
    return {"sent": True, "recipient": recipient, "subject": subject, "jobs": len(job_ids),
            "message_id": message_id, "counts": data["counts"], "total_ever_sent": len(ledger["job_ids"])}
