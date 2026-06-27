"""Fenced Postgres job queue: enqueue (idempotent), claim (SKIP LOCKED +
fencing token). Heartbeat/finalize/reclaim land in Task 5."""
from __future__ import annotations

import json
import socket
from datetime import datetime, timezone
from typing import Optional

import psycopg
from psycopg.types.json import Json

from argus.v2.queue.models import Job

_JOB_COLS = ("id, request_id, event_id, conversation_id, team_id, role, stage, "
             "kind, status, attempts, max_attempts, claim_token, exec_snapshot, payload")


def _row_to_job(row) -> Job:
    return Job(
        id=str(row[0]), request_id=_s(row[1]), event_id=_s(row[2]),
        conversation_id=_s(row[3]), team_id=row[4], role=row[5], stage=row[6],
        kind=row[7], status=row[8], attempts=row[9], max_attempts=row[10],
        claim_token=_s(row[11]), exec_snapshot=row[12], payload=row[13],
    )


def _s(v) -> Optional[str]:
    return None if v is None else str(v)


def enqueue(conn: psycopg.Connection, *, team_id: str, kind: str, role: str,
            stage: int, idempotency_key: str, exec_snapshot: dict, payload: dict,
            request_id: Optional[str] = None, event_id: Optional[str] = None,
            conversation_id: Optional[str] = None,
            run_after: Optional[datetime] = None, max_attempts: int = 3) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO jobs (request_id, event_id, conversation_id, team_id, role,
                              stage, kind, idempotency_key, exec_snapshot, payload,
                              max_attempts, run_after)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, COALESCE(%s, now()))
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            (request_id, event_id, conversation_id, team_id, role, stage, kind,
             idempotency_key, Json(exec_snapshot), Json(payload), max_attempts, run_after),
        )
        row = cur.fetchone()
        if row:
            return str(row[0])
        cur.execute("SELECT id FROM jobs WHERE idempotency_key=%s", (idempotency_key,))
        return str(cur.fetchone()[0])


def claim(conn: psycopg.Connection, worker_id: str, *, lease_seconds: int = 120) -> Optional[Job]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE jobs SET
                status='claimed',
                claim_token=gen_random_uuid(),
                claimed_by=%s,
                claimed_at=now(),
                heartbeat_at=now(),
                lease_expires_at=now() + make_interval(secs => %s),
                updated_at=now()
            WHERE id = (
                SELECT id FROM jobs
                WHERE status='pending' AND run_after <= now()
                -- Owner-facing work jumps the queue: a chat reply (converse) and
                -- a signal triage must not wait behind background pipeline/research
                -- jobs during a monitoring flood. FIFO within each priority.
                ORDER BY (CASE kind WHEN 'converse' THEN 0 WHEN 'triage' THEN 1
                                    ELSE 2 END), run_after
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING {_JOB_COLS}
            """,
            (f"{worker_id}@{socket.gethostname()}", lease_seconds),
        )
        row = cur.fetchone()
        return _row_to_job(row) if row else None


from argus.v2.queue.models import ActionIntent, RunRecord  # noqa: E402


def heartbeat(conn: psycopg.Connection, job_id: str, claim_token: str, *,
              lease_seconds: int = 120) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE jobs SET heartbeat_at=now(),
                            lease_expires_at=now() + make_interval(secs => %s),
                            status='running', updated_at=now()
            WHERE id=%s AND claim_token=%s AND status IN ('claimed','running')
            """,
            (lease_seconds, job_id, claim_token),
        )
        return cur.rowcount == 1


def finalize(conn: psycopg.Connection, job_id: str, claim_token: str, *,
             status: str, result: dict, run: RunRecord,
             actions: list) -> bool:
    """CAS finalize: only the lease holder may complete. Writes the run row and
    any action intents in the SAME transaction as the job's terminal state."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE jobs SET status=%s, result=%s, updated_at=now()
            WHERE id=%s AND claim_token=%s AND status IN ('claimed','running')
            RETURNING attempts
            """,
            (status, Json(result), job_id, claim_token),
        )
        row = cur.fetchone()
        if not row:
            return False  # fenced out: stale token or already reclaimed
        attempt = row[0]
        cur.execute(
            """
            INSERT INTO runs (job_id, attempt, claim_token, role, engine, model,
                              prompt, output, cost_source, cost_usd, status, ended_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
            """,
            (job_id, attempt, claim_token, run.role, run.engine, run.model,
             run.prompt, run.output, run.cost_source, run.cost_usd, run.status),
        )
        cur.execute("SELECT request_id, team_id FROM jobs WHERE id=%s", (job_id,))
        request_id, team_id = cur.fetchone()
        for a in actions:
            cur.execute(
                """
                INSERT INTO actions (request_id, job_id, team_id, type, risk,
                                     destination_ref, idempotency_key, payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                (request_id, job_id, team_id, a.type, a.risk, a.destination_ref,
                 a.idempotency_key, Json(a.payload)),
            )
    return True


def reclaim_expired(conn: psycopg.Connection) -> int:
    """Requeue jobs whose lease expired (worker crash). Rotate the token so the
    old holder is fenced. To dead past max_attempts."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE jobs SET
                status = CASE WHEN attempts + 1 >= max_attempts THEN 'dead' ELSE 'pending' END,
                attempts = attempts + 1,
                claim_token = gen_random_uuid(),
                claimed_by = NULL, claimed_at = NULL,
                lease_expires_at = NULL, heartbeat_at = NULL,
                run_after = now(),
                updated_at = now()
            WHERE status IN ('claimed','running') AND lease_expires_at < now()
            """,
        )
        return cur.rowcount
