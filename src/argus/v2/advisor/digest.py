"""Daily advisor digest runner."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from argus.v2.advisor import state
from argus.v2.advisor import tick


def run(*, now: datetime | None = None, groups: list[str] | None = None,
        engine_runner=None, sender=None) -> int:
    now = now or datetime.now(timezone.utc)
    groups = groups if groups is not None else [
        g.strip() for g in os.environ.get("ARGUS_ADVISOR_GROUPS", "").split(",") if g.strip()
    ]
    engine_runner = engine_runner or tick._run_engine
    sender = sender or tick._send
    posted_or_recorded = 0
    for jid in groups:
        if _digest_group(jid, now, engine_runner, sender):
            posted_or_recorded += 1
    return posted_or_recorded


def _digest_group(jid: str, now: datetime, engine_runner, sender) -> bool:
    day = (now.astimezone(timezone.utc).date() - timedelta(days=1)).isoformat()
    start = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())
    end = start + 86400
    if state.digest_seen(jid, day):
        return False
    messages = [
        row for row in state.messages(jid)
        if start <= int(row.get("ts") or 0) < end
    ]
    min_messages = _cfg_int("DIGEST_MIN_MESSAGES", 8)
    if len(messages) < min_messages:
        state.record_digest(jid, {"date": day, "message_count": len(messages),
                                  "posted": False, "reason": "quiet"})
        return True

    body = "\n".join(f"{row.get('push_name') or 'user'}: {row.get('body') or ''}" for row in messages)
    seeds = "\n".join(
        row.get("seed_topic", "") for row in state.digests(jid)[-10:] if row.get("seed_topic")
    )
    prompt = tick._contract("digest") + f"\n\nPrevious seed topics:\n{seeds}\n\nMessages from {day}:\n{body}"
    out = (engine_runner(prompt) or "").strip()
    if not out or out == "SKIP":
        state.record_digest(jid, {"date": day, "message_count": len(messages),
                                  "posted": False, "reason": "engine_skip"})
        return True

    recap, seed = _split_digest(out)
    posted = bool(recap and sender(jid, recap, None, None, None))
    reason = None
    if posted and seed:
        sender(jid, seed, None, None, None)
    if not posted:
        reason = "delivery"
    state.record_digest(jid, {
        "date": day,
        "message_count": len(messages),
        "posted": posted,
        "seed_topic": seed[:80],
        "reason": reason,
    })
    return True


def _split_digest(text: str) -> tuple[str, str]:
    marker = "---SEED---"
    if marker not in text:
        return text.strip(), ""
    recap, seed = text.split(marker, 1)
    return recap.strip(), seed.strip()


def _cfg_int(name: str, default: int) -> int:
    value = os.environ.get(f"ARGUS_ADVISOR_{name}", "")
    return int(value) if value.isdigit() else default
