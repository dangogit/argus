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


def edit(cfg, destination_ref: str, message_id: str, text: str):
    """Edit a previously sent message in place. Returns the message id on
    success, or None when the destination can't be resolved or its channel has
    no update() (non-editable channel like whatsapp/email) - so the caller can
    fall back to a fresh send()."""
    if ":" not in (destination_ref or "") or not message_id:
        return None
    ctype, chat_id = destination_ref.split(":", 1)
    if ctype not in REGISTRY:
        return None
    from argus.v2.channels.router import team_for
    route = team_for(cfg, ctype, chat_id)
    if not route:
        return None
    _team, binding = route
    update = getattr(REGISTRY[ctype](), "update", None)
    if update is None:
        return None
    return update(binding, message_id, text)


def channel_supports_edit(channel_type: str) -> bool:
    """True if the channel type can edit a sent message in place (Slack,
    Telegram, Discord, fake). Used to decide whether to drive a live status
    line vs a one-shot receipt."""
    cls = REGISTRY.get(channel_type)
    return cls is not None and hasattr(cls, "update")
