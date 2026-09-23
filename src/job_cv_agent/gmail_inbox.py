"""Gmail token paths and client, written as a minimal stand-in for this public extract.

The private module is larger and is not published. This stand-in differs from it as
follows:

- `build_gmail_service` only refreshes a stored token, or raises. It never starts a
  browser consent flow; the private helper falls back to one when no usable token
  exists. Consent is only ever run by hand, through scripts/reauthorise_gmail.py.
- Only the token paths, the scopes and the client builder are here.
- The Google client libraries are imported only when a client is built, so the
  tests and the demo do not need them.
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CREDENTIALS_PATH = PROJECT_ROOT / "local_data" / "google_oauth_credentials.json"
DEFAULT_TOKEN_PATH = PROJECT_ROOT / "local_data" / "google_oauth_token.json"
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


def build_gmail_service(credentials_path: Path = DEFAULT_CREDENTIALS_PATH,
                        token_path: Path = DEFAULT_TOKEN_PATH):
    """A Gmail client from a stored token, refreshed if needed. Never a consent flow."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_path = Path(token_path)
    if not token_path.exists():
        raise FileNotFoundError(f"No Gmail token at {token_path}; run scripts/reauthorise_gmail.py yourself.")
    credentials = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
    if not credentials.valid:
        if not credentials.refresh_token:
            raise PermissionError("The stored Gmail token cannot be refreshed; run scripts/reauthorise_gmail.py yourself.")
        credentials.refresh(Request())
        token_path.write_text(credentials.to_json(), encoding="utf-8")
        token_path.chmod(0o600)
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)
