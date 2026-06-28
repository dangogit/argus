"""Global daily LLM cost ceiling.

A signal storm (a connector emitting hundreds of events) can enqueue hundreds of
paid jobs. `runs.cost_usd` was recorded but never enforced. When rolling 24h
spend reaches the configured cap, the orchestrator stops opening NEW work
(in-flight jobs still finish), so the bill cannot run away and erode trust.
None disables the cap, which is the default - existing installs are unaffected.
"""
from __future__ import annotations

import logging

log = logging.getLogger("argus.orchestrator")

# cost_usd is free-text (it may be '', 'unpriced'-era blanks, or a decimal).
# Only sum rows that are a plain decimal so a bad value can't poison the SUM.
_NUMERIC = r"^[0-9]+(\.[0-9]+)?$"


def daily_cost_usd(conn) -> float:
    """Sum of priced run cost over the last 24 hours."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(cost_usd::numeric), 0) FROM runs "
            "WHERE ended_at > now() - interval '24 hours' AND cost_usd ~ %s",
            (_NUMERIC,))
        return float(cur.fetchone()[0])


def ceiling(cfg) -> float | None:
    """Configured cap, or None when unset/zero (disabled)."""
    cap = getattr(cfg.company.defaults, "max_daily_cost_usd", None)
    return float(cap) if cap else None


def over_budget(conn, cfg) -> bool:
    """True when a cap is set and 24h spend has reached it."""
    cap = ceiling(cfg)
    if cap is None:
        return False
    return daily_cost_usd(conn) >= cap
