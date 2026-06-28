"""Email channel: outbound delivery via SMTP. Inbound email is handled by the
email_imap connector, so this adapter only sends - together they form a generic
email gateway. SMTP config comes from the environment (no creds in YAML);
stdlib smtplib, no new dependency."""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

from argus.v2.channels.base import register


def build_message(to_addr: str, text: str, *, from_addr: str, subject: str = "Argus") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(text)
    return msg


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
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls()
            if user and password:
                s.login(user, password)
            s.send_message(msg)
        return binding.channel_id
