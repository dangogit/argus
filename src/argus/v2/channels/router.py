"""Route an inbound chat to its team, and turn parsed messages into durable
events under one ongoing conversation per chat."""
from __future__ import annotations

import logging
import re

from argus.v2.channels.base import REGISTRY
from argus.v2.ingress import events

logger = logging.getLogger(__name__)


def team_for(cfg, channel_type: str, chat_id: str, text: str | None = None):
    matches = _matching_teams(cfg, channel_type, chat_id)
    if len(matches) <= 1:
        return matches[0] if matches else None
    if text:
        for team, binding in sorted(matches, key=lambda item: len(item[0]), reverse=True):
            if _mentions_team(text, team):
                return team, binding
    return None


def inbound(conn, cfg, channel_type: str, raw, secret=None) -> int:
    adapter = REGISTRY[channel_type]()
    n = 0
    for m in adapter.parse_inbound(raw, secret):
        route = team_for(cfg, channel_type, m.chat_id, m.text)
        if not route:
            _log_unrouted(cfg, channel_type, m)
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


def _matching_teams(cfg, channel_type: str, chat_id: str):
    matches = []
    for t in cfg.teams:
        for ch in t.channels:
            if ch.type == channel_type and ch.channel_id == chat_id:
                matches.append((t.name, ch))
    return matches


def _log_unrouted(cfg, channel_type: str, message) -> None:
    matches = _matching_teams(cfg, channel_type, message.chat_id)
    if len(matches) <= 1:
        return
    logger.warning(
        "dropping inbound message: ambiguous channel route type=%s chat_id=%s teams=%s text_present=%s media_count=%s",
        channel_type,
        message.chat_id,
        ",".join(team for team, _binding in matches),
        bool((message.text or "").strip()),
        len(message.media),
    )


def _mentions_team(text: str, team_name: str) -> bool:
    separators = r"[-_\s]+"
    parts = [re.escape(part) for part in re.split(r"[-_\s]+", team_name.strip()) if part]
    if not parts:
        return False
    pattern = separators.join(parts)
    return bool(re.search(rf"(?<![\w-]){pattern}(?![\w-])", text, re.IGNORECASE))


def _allowed_sender(binding, sender_ref: str) -> bool:
    owners = (binding.config or {}).get("owner_ids") or []
    if isinstance(owners, str):
        owners = [owners]
    if not owners:
        return True
    sender = (sender_ref or "").strip()
    allowed = {str(owner).strip() for owner in owners}
    return bool(sender and sender in allowed)
