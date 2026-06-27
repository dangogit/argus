"""IMAP mailbox connector: one signal per new message beyond the UID cursor.
parse() (pure, gate-tested) maps fetched tuples to signals; fetch() (network)
pulls UNSEEN messages over IMAP."""
from __future__ import annotations

from argus.v2.connectors.base import Signal, register


@register
class EmailConnector:
    type = "email"

    @staticmethod
    def parse(raw, state: dict):
        last_uid = int(state.get("last_uid", 0))
        signals, newest = [], last_uid
        for uid, headers, body in raw:
            if int(uid) <= last_uid:
                continue
            signals.append(Signal(
                fingerprint=headers.get("Message-ID", str(uid)),
                payload={"from": headers.get("From"), "subject": headers.get("Subject"),
                         "body": body}))
            newest = max(newest, int(uid))
        return signals, {"last_uid": newest}

    def fetch(self, source, state: dict):  # pragma: no cover
        # NETWORK -- not gate-run. Returns [(uid, headers, body), ...] for new mail.
        import email
        import imaplib
        cfg = source.config or {}
        last_uid = int(state.get("last_uid", 0))
        m = imaplib.IMAP4_SSL(cfg["host"])
        m.login(cfg["user"], source.secret)
        m.select(cfg.get("folder", "INBOX"))
        typ, data = m.uid("search", None, f"(UID {last_uid + 1}:*)")
        out = []
        for uid in (data[0].split() if data and data[0] else []):
            if int(uid) <= last_uid:
                continue
            typ, msg_data = m.uid("fetch", uid, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode(errors="replace")
                        break
            else:
                body = msg.get_payload(decode=True).decode(errors="replace")
            out.append((int(uid), dict(msg.items()), body))
        m.logout()
        return out

    def poll(self, source, state: dict):
        return self.parse(self.fetch(source, state), state)
