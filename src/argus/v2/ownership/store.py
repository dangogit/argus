from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from argus.v2.ownership.models import LEGAL_TRANSITIONS, Obligation

_COLUMNS = """
    id, team_id, kind, fingerprint, title, status, priority,
    request_id, action_id, provider_ref, source_ref, definition_of_done,
    evidence, attempts, next_check_at, blocked_reason, created_at,
    updated_at, completed_at
"""


def _obligation(row: dict[str, Any] | None) -> Obligation | None:
    return Obligation(**row) if row is not None else None


def _required(row: dict[str, Any] | None, obligation_id: UUID | str) -> Obligation:
    item = _obligation(row)
    if item is None:
        raise ValueError(f"obligation not found: {obligation_id}")
    return item


def upsert(
    conn: psycopg.Connection,
    *,
    team_id: str,
    kind: str,
    fingerprint: str,
    title: str,
    source_ref: str | None,
    definition_of_done: dict[str, Any],
) -> Obligation:
    if conn.autocommit:
        raise ValueError("obligation upsert requires autocommit=False")
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            INSERT INTO team_obligations
              (team_id, kind, fingerprint, title, source_ref, definition_of_done)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (team_id, fingerprint) DO NOTHING
            RETURNING {_COLUMNS}
            """,
            (team_id, kind, fingerprint, title, source_ref, Jsonb(definition_of_done)),
        )
        row = cur.fetchone()
        if row is not None:
            cur.execute(
                """
                INSERT INTO team_obligation_events
                  (obligation_id, from_status, to_status, reason, evidence)
                VALUES (%s, NULL, 'open', 'obligation opened', '{}'::jsonb)
                """,
                (row["id"],),
            )
        else:
            cur.execute(
                f"""
                SELECT {_COLUMNS}
                FROM team_obligations
                WHERE team_id=%s AND fingerprint=%s
                """,
                (team_id, fingerprint),
            )
            row = cur.fetchone()
    return _required(row, f"{team_id}/{fingerprint}")


def get(conn: psycopg.Connection, obligation_id: UUID | str) -> Obligation | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM team_obligations WHERE id=%s",
            (obligation_id,),
        )
        return _obligation(cur.fetchone())


def list_due(
    conn: psycopg.Connection,
    *,
    team_id: str | None = None,
    statuses: tuple[str, ...] | None = None,
    limit: int = 50,
) -> list[Obligation]:
    team_filter = "AND team_id=%s" if team_id is not None else ""
    status_filter = "AND status = ANY(%s)" if statuses is not None else ""
    params: list[Any] = []
    if team_id is not None:
        params.append(team_id)
    if statuses is not None:
        if not statuses:
            return []
        unknown = set(statuses) - set(LEGAL_TRANSITIONS)
        if unknown:
            raise ValueError(f"unknown obligation statuses: {sorted(unknown)!r}")
        params.append(list(statuses))
    params.append(limit)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT {_COLUMNS}
            FROM team_obligations
            WHERE status NOT IN ('done', 'failed')
              AND next_check_at <= clock_timestamp()
              {team_filter}
              {status_filter}
            ORDER BY priority DESC, next_check_at, created_at, id
            LIMIT %s
            """,
            tuple(params),
        )
        return [Obligation(**row) for row in cur.fetchall()]


def transition(
    conn: psycopg.Connection,
    obligation_id: UUID | str,
    *,
    to_status: str,
    reason: str,
    evidence: dict[str, Any] | None = None,
) -> Obligation:
    if conn.autocommit:
        raise ValueError("obligation transition requires autocommit=False")

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT {_COLUMNS}
            FROM team_obligations
            WHERE id=%s
            FOR UPDATE
            """,
            (obligation_id,),
        )
        current = _required(cur.fetchone(), obligation_id)
        event_evidence = evidence or {}
        if to_status == current.status:
            cur.execute(
                """
                SELECT to_status, reason, evidence
                FROM team_obligation_events
                WHERE obligation_id=%s
                ORDER BY id DESC
                LIMIT 1
                """,
                (obligation_id,),
            )
            latest_event = cur.fetchone()
            if latest_event is not None and (
                latest_event["to_status"] == to_status
                and latest_event["reason"] == reason
                and latest_event["evidence"] == event_evidence
            ):
                return current
            if current.status in {"done", "failed"}:
                raise ValueError(
                    f"terminal obligation update differs: {current.status} -> {to_status}"
                )
        elif to_status not in LEGAL_TRANSITIONS.get(current.status, frozenset()):
            raise ValueError(f"illegal obligation transition: {current.status} -> {to_status}")

        terminal = to_status in {"done", "failed"}
        cur.execute(
            f"""
            UPDATE team_obligations
            SET status=%s,
                evidence=evidence || %s,
                blocked_reason=CASE WHEN %s='blocked' THEN %s ELSE NULL END,
                completed_at=CASE WHEN %s THEN clock_timestamp() ELSE NULL END,
                updated_at=clock_timestamp()
            WHERE id=%s
            RETURNING {_COLUMNS}
            """,
            (
                to_status,
                Jsonb(event_evidence),
                to_status,
                reason,
                terminal,
                obligation_id,
            ),
        )
        updated = _required(cur.fetchone(), obligation_id)
        cur.execute(
            """
            INSERT INTO team_obligation_events
              (obligation_id, from_status, to_status, reason, evidence)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (obligation_id, current.status, to_status, reason, Jsonb(event_evidence)),
        )
        return updated


def link_request(
    conn: psycopg.Connection,
    obligation_id: UUID | str,
    request_id: UUID | str,
) -> Obligation:
    return _link(conn, obligation_id, field="request_id", value=request_id)


def link_action(
    conn: psycopg.Connection,
    obligation_id: UUID | str,
    action_id: UUID | str,
) -> Obligation:
    return _link(conn, obligation_id, field="action_id", value=action_id)


def _link(
    conn: psycopg.Connection,
    obligation_id: UUID | str,
    *,
    field: str,
    value: UUID | str,
) -> Obligation:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            UPDATE team_obligations
            SET {field}=%s, updated_at=clock_timestamp()
            WHERE id=%s
            RETURNING {_COLUMNS}
            """,
            (value, obligation_id),
        )
        return _required(cur.fetchone(), obligation_id)


def increment_attempts(
    conn: psycopg.Connection,
    obligation_id: UUID | str,
) -> Obligation:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            UPDATE team_obligations
            SET attempts=attempts + 1, updated_at=clock_timestamp()
            WHERE id=%s
            RETURNING {_COLUMNS}
            """,
            (obligation_id,),
        )
        return _required(cur.fetchone(), obligation_id)
