"""Send a prepared message through the Gmail API. A minimal stand-in for this public
extract.

The private module is larger and is not published. Only `send_job_email` is here,
copied unchanged. The private module's message builder, which attaches a CV and
depends on private CV file naming, is deliberately left out: the digest builds its
own plain-text message.
"""

import base64
from email.message import EmailMessage


def send_job_email(service, message: EmailMessage) -> str:
    """Send a prepared MIME message and return Gmail's message ID."""
    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    result = (
        service.users()
        .messages()
        .send(userId="me", body={"raw": raw_message})
        .execute()
    )
    return result["id"]
