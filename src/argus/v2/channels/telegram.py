"""Telegram via long-poll (getUpdates). parse_inbound (gate-tested) maps updates
to messages; fetch/send (seams) hit the Bot API. parse_with_offset also returns
the next getUpdates offset (cursor) for the poll driver."""
from __future__ import annotations

from argus.v2.channels.base import InboundMessage, register


@register
class TelegramChannel:
    type = "telegram"

    @staticmethod
    def parse_with_offset(raw):
        results = raw.get("result", []) if isinstance(raw, dict) else (raw or [])
        msgs, max_id = [], None
        for u in results:
            uid = u.get("update_id")
            max_id = uid if max_id is None else max(max_id, uid)
            msg = u.get("message")  # ignore edited_message / others
            if not msg or "text" not in msg:
                continue
            msgs.append(InboundMessage(
                chat_id=str(msg["chat"]["id"]), text=msg["text"],
                dedup_key=str(u["update_id"]),
                sender=(msg.get("from") or {}).get("username", "")))
        offset = (max_id + 1) if max_id is not None else None
        return msgs, offset

    def parse_inbound(self, raw, secret=None):
        return self.parse_with_offset(raw)[0]

    def send(self, binding, text: str) -> str:  # pragma: no cover
        import httpx
        r = httpx.post(f"https://api.telegram.org/bot{binding.secret}/sendMessage",
                       json={"chat_id": binding.channel_id, "text": text}, timeout=20)
        r.raise_for_status()
        return str(r.json().get("result", {}).get("message_id", ""))
