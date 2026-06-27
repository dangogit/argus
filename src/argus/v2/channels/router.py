"""Route an inbound chat to its team, and turn parsed messages into durable
events under one ongoing conversation per chat."""
from __future__ import annotations

from argus.v2.channels.base import REGISTRY
from argus.v2.ingress import events


def team_for(cfg, channel_type: str, chat_id: str):
    for t in cfg.teams:
        for ch in t.channels:
            if ch.type == channel_type and ch.channel_id == chat_id:
                return t.name, ch
    return None


def inbound(conn, cfg, channel_type: str, raw, secret=None) -> int:
    adapter = REGISTRY[channel_type]()
    n = 0
    for m in adapter.parse_inbound(raw, secret):
        route = team_for(cfg, channel_type, m.chat_id)
        if not route:
            continue
        team, _binding = route
        if not _allowed_sender(_binding, m.sender_ref):
            continue
        prepare = getattr(adapter, "prepare_inbound", None)
        if prepare:
            m = prepare(m, raw, _binding)
        key = f"{channel_type}:{m.chat_id}"
        media = [item for item in m.media if item.get("src")]
        events.ingest_message(conn, cfg, team=team, source=key,
                              dedup_key=m.dedup_key, text=m.text, media=media,
                              conversation_key=key, metadata=m.metadata)
        n += 1
    return n


def _allowed_sender(binding, sender_ref: str) -> bool:
    owners = (binding.config or {}).get("owner_ids") or []
    if isinstance(owners, str):
        owners = [owners]
    if not owners:
        return True
    sender = (sender_ref or "").strip()
    allowed = {str(owner).strip() for owner in owners}
    return bool(sender and sender in allowed)
