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


def daily_cost_usd(conn, team_id: str | None = None) -> float:
    """Sum of priced run cost over the last 24 hours. Scoped to a team when
    team_id is given (runs join jobs for the team), else company-wide."""
    with conn.cursor() as cur:
        if team_id is None:
            cur.execute(
                "SELECT COALESCE(SUM(cost_usd::numeric), 0) FROM runs "
                "WHERE ended_at > now() - interval '24 hours' AND cost_usd ~ %s",
                (_NUMERIC,))
        else:
            cur.execute(
                "SELECT COALESCE(SUM(r.cost_usd::numeric), 0) FROM runs r "
                "JOIN jobs j ON j.id = r.job_id "
                "WHERE r.ended_at > now() - interval '24 hours' AND r.cost_usd ~ %s "
                "AND j.team_id = %s",
                (_NUMERIC, team_id))
        return float(cur.fetchone()[0])


def ceiling(cfg) -> float | None:
    """Company-wide cap, or None when unset/zero (disabled)."""
    cap = getattr(cfg.company.defaults, "max_daily_cost_usd", None)
    return float(cap) if cap else None


def team_ceiling(cfg, team_id: str) -> float | None:
    """Per-team cap, or None when unset/zero (disabled)."""
    try:
        cap = getattr(cfg.team(team_id), "max_daily_cost_usd", None)
    except (KeyError, AttributeError):
        return None
    return float(cap) if cap else None


def over_budget(conn, cfg, team_id: str | None = None) -> bool:
    """True when a relevant cap is set and reached. With team_id, the team's own
    cap is checked first (only that team pauses), then the company cap."""
    if team_id is not None:
        cap = team_ceiling(cfg, team_id)
        if cap is not None and daily_cost_usd(conn, team_id) >= cap:
            return True
    company = ceiling(cfg)
    if company is None:
        return False
    return daily_cost_usd(conn) >= company
