"""Team-scoped ownership reconciliation with transaction advisory locks."""
from __future__ import annotations

from dataclasses import dataclass

import psycopg

from argus.v2.ownership import code, maintenance, store


_RECONCILABLE_STATUSES = (
    "awaiting_pr",
    "awaiting_merge",
    "awaiting_deploy",
    "verifying",
    "awaiting_approval",
)


@dataclass(frozen=True)
class CycleResult:
    teams: int = 0
    reconciled: int = 0
    actions_proposed: int = 0
    completed: int = 0
    blocked: int = 0
    skipped_locked: int = 0


def run(
    conn: psycopg.Connection,
    cfg,
    *,
    team_id=None,
    runner=None,
    http_get: code.PinnedHTTPGet | None = None,
    resolver=None,
) -> CycleResult:
    if conn.autocommit:
        raise ValueError("ownership cycle requires autocommit=False")
    teams = _teams(cfg, team_id)
    counts = {
        "teams": len(teams),
        "reconciled": 0,
        "actions_proposed": 0,
        "completed": 0,
        "blocked": 0,
        "skipped_locked": 0,
    }
    for team in teams:
        if not _try_team_lock(conn, team.name):
            counts["skipped_locked"] += 1
            continue
        due = store.list_due(
            conn,
            team_id=team.name,
            statuses=_RECONCILABLE_STATUSES,
            limit=team.ownership.max_active_obligations,
        )
        for obligation in due:
            if obligation.kind not in {"code", "maintenance"}:
                continue
            result = code.reconcile(
                conn,
                cfg,
                obligation,
                runner=runner,
                http_get=http_get,
                resolver=resolver,
            )
            counts["reconciled"] += 1
            counts["actions_proposed"] += result.actions_proposed
            counts["completed"] += result.completed
            counts["blocked"] += result.blocked
        candidates = maintenance.collect_candidates(conn, cfg, team.name)
        maintenance.dispatch_one(conn, cfg, team.name, candidates)
    return CycleResult(**counts)


def _teams(cfg, team_id):
    if team_id is not None:
        team = cfg.team(team_id)
        return [team] if team.ownership.enabled else []
    return [team for team in cfg.teams if team.ownership.enabled]


def _try_team_lock(conn: psycopg.Connection, team_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_try_advisory_xact_lock("
            "hashtext('argus-owner:' || %s))",
            (team_id,),
        )
        row = cur.fetchone()
    return bool(row and row[0] is True)
