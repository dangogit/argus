"""Team-scoped ownership reconciliation with transaction advisory locks."""
from __future__ import annotations

from dataclasses import dataclass

import psycopg

from argus.v2.ownership import code, store


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
    http_get=None,
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
            limit=team.ownership.max_active_obligations,
        )
        for obligation in due:
            if obligation.kind != "code":
                continue
            result = code.reconcile(
                conn,
                cfg,
                obligation,
                runner=runner,
                http_get=http_get,
            )
            counts["reconciled"] += 1
            counts["actions_proposed"] += result.actions_proposed
            counts["completed"] += result.completed
            counts["blocked"] += result.blocked
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
