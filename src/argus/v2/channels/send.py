"""Deliver outbound text to a channel destination ('<type>:<chat_id>') by
resolving the binding + adapter and calling adapter.send (a network seam)."""
from __future__ import annotations

from argus.v2.channels.base import REGISTRY


def deliver(cfg, destination_ref: str, text: str):
    if ":" not in (destination_ref or ""):
        return None
    ctype, chat_id = destination_ref.split(":", 1)
    if ctype not in REGISTRY:
        return None
    from argus.v2.channels.router import team_for
    route = team_for(cfg, ctype, chat_id)
    if not route:
        return None
    _team, binding = route
    return REGISTRY[ctype]().send(binding, text)
