"""Discord via REST poll (GET /channels/{id}/messages?after=). parse_with_offset
(gate-tested) maps the message list to InboundMessage + the next `after` cursor
(the highest snowflake id). send posts back. Bot-authored messages are skipped
so Argus never reacts to itself or other bots."""
from __future__ import annotations

from argus.v2.channels.base import InboundMessage, register

API = "https://discord.com/api/v10"


@register
class DiscordChannel:
    type = "discord"

    @staticmethod
    def parse_with_offset(raw):
        # Discord returns newest-first; an explicit {"messages": [...]} wrapper is
        # also accepted for convenience.
        items = raw if isinstance(raw, list) else (raw.get("messages") or [])
        msgs, max_id = [], None
        for m in items:
            mid = m.get("id")
            if mid is not None:
                try:
                    mid_int = int(mid)
                except (TypeError, ValueError):
                    continue  # non-numeric snowflake: skip, don't abort the batch
                if max_id is None or mid_int > int(max_id):
                    max_id = mid
            author = m.get("author") or {}
            if author.get("bot"):
                continue  # never react to bot messages (including our own)
            content = m.get("content") or ""
            if not content:
                continue
            msgs.append(InboundMessage(
                chat_id=str(m.get("channel_id", "")), text=content,
                dedup_key=str(mid), sender=author.get("username", "")))
        msgs.reverse()  # chronological order for processing
        offset = str(max_id) if max_id is not None else None
        return msgs, offset

    def parse_inbound(self, raw, secret=None):
        return self.parse_with_offset(raw)[0]

    def send(self, binding, text: str) -> str:  # pragma: no cover (network seam)
        import httpx
        r = httpx.post(f"{API}/channels/{binding.channel_id}/messages",
                       headers={"Authorization": f"Bot {binding.secret}"},
                       json={"content": text}, timeout=20)
        r.raise_for_status()
        return str(r.json().get("id", ""))

    def update(self, binding, message_id: str, text: str) -> str:  # pragma: no cover (network seam)
        import httpx
        r = httpx.patch(f"{API}/channels/{binding.channel_id}/messages/{message_id}",
                        headers={"Authorization": f"Bot {binding.secret}"},
                        json={"content": text}, timeout=20)
        r.raise_for_status()
        return str(r.json().get("id", message_id))
