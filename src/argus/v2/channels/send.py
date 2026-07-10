"""Deliver outbound text to a channel destination ('<type>:<chat_id>') by
resolving the binding + adapter and calling adapter.send (a network seam)."""
from __future__ import annotations

from argus.v2.channels.base import REGISTRY


def deliver(cfg, destination_ref: str, text: str, *, thread_ts=None):
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
    adapter = REGISTRY[ctype]()
    if thread_ts is not None:
        # Thread the reply when the channel supports it (Slack); channels whose
        # send() has no thread_ts parameter fall back to a plain send.
        import inspect
        if "thread_ts" in inspect.signature(adapter.send).parameters:
            return adapter.send(binding, text, thread_ts=thread_ts)
    return adapter.send(binding, text)


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


def set_typing(cfg, destination_ref: str, thread_ts: str, status: str,
               loading_messages=None):
    """Best-effort native typing indicator (Slack assistant dots). No-op (returns
    None) when the destination can't be resolved or its channel has no
    set_typing() - so every non-Slack channel silently skips it."""
    if ":" not in (destination_ref or "") or not thread_ts:
        return None
    ctype, chat_id = destination_ref.split(":", 1)
    if ctype not in REGISTRY:
        return None
    from argus.v2.channels.router import team_for
    route = team_for(cfg, ctype, chat_id)
    if not route:
        return None
    _team, binding = route
    fn = getattr(REGISTRY[ctype](), "set_typing", None)
    if fn is None:
        return None
    return fn(binding, thread_ts, status, loading_messages)


def channel_supports_edit(channel_type: str) -> bool:
    """True if the channel type can edit a sent message in place (Slack,
    Telegram, Discord, fake). Used to decide whether to drive a live status
    line vs a one-shot receipt."""
    cls = REGISTRY.get(channel_type)
    return cls is not None and hasattr(cls, "update")
