"""Deterministic test channel. parse_inbound reads a simple dict; send records
to SENT (cleared by tests)."""
from __future__ import annotations

from argus.v2.channels.base import InboundMessage, register

SENT: list = []


@register
class FakeChannel:
    type = "fake"

    def parse_inbound(self, raw, secret=None):
        items = raw if isinstance(raw, list) else [raw]
        return [InboundMessage(chat_id=str(i["chat_id"]), text=i.get("text", ""),
                               dedup_key=str(i["id"]), sender=i.get("from", ""),
                               media=i.get("media", [])) for i in items]

    def send(self, binding, text: str) -> str:
        SENT.append((binding.channel_id, text))
        return f"fake-{len(SENT)}"
