"""Email channel: outbound delivery via SMTP. Inbound email is handled by the
email_imap connector, so this adapter only sends - together they form a generic
email gateway. SMTP config comes from the environment (no creds in YAML);
stdlib smtplib, no new dependency."""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid


def _clean_addr(addr: str) -> str:
    # Reject header injection: an address with CR/LF could smuggle extra headers
    # (Bcc, etc.) into the message.
    if "\r" in addr or "\n" in addr:
        raise ValueError("invalid email address (contains newline)")
    return addr


def build_message(to_addr: str, text: str, *, from_addr: str, subject: str = "Argus") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = _clean_addr(from_addr)
    msg["To"] = _clean_addr(to_addr)
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()
    msg.set_content(text)
    return msg


from argus.v2.channels.base import register  # noqa: E402


@register
class EmailChannel:
    type = "email"

    def parse_inbound(self, raw, secret=None):
        return []  # inbound email arrives via the email_imap connector

    def send(self, binding, text: str) -> str:  # pragma: no cover (network seam)
        host = os.environ.get("ARGUS_SMTP_HOST", "localhost")
        port = int(os.environ.get("ARGUS_SMTP_PORT", "587"))
        user = os.environ.get("ARGUS_SMTP_USER")
        password = os.environ.get("ARGUS_SMTP_PASSWORD")
        from_addr = os.environ.get("ARGUS_SMTP_FROM", user or "argus@localhost")
        msg = build_message(binding.channel_id, text, from_addr=from_addr)
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20, context=ctx) as s:
                if user and password:
                    s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                # Require TLS before authenticating: a STARTTLS-stripping MITM
                # must not get our credentials in cleartext.
                s.starttls(context=ctx)
                if user and password:
                    s.login(user, password)
                s.send_message(msg)
        return msg["Message-ID"]
