"""Fenced Postgres job queue: enqueue (idempotent), claim (SKIP LOCKED +
fencing token). Heartbeat/finalize/reclaim land in Task 5."""
from __future__ import annotations

import json
import logging
import socket
from datetime import datetime, timezone
from typing import Optional

import psycopg
from psycopg.types.json import Json

from argus.v2.queue.models import Job

log = logging.getLogger("argus.queue")

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


# Owner-facing chat jobs. A dedicated worker lane claims only these so a chat
# reply never waits behind (or is frozen by) a slow/hung pipeline build running
# in another lane. The pipeline lane claims everything else.
CHAT_KINDS = ("converse", "triage")


def claim(conn: psycopg.Connection, worker_id: str, *, lease_seconds: int = 120,
          include_kinds=None, exclude_kinds=None) -> Optional[Job]:
    """Claim one pending job (fenced, SKIP LOCKED). A worker can restrict itself
    to a lane: include_kinds claims only those kinds (the chat lane), exclude_kinds
    skips them (the pipeline lane). With neither, claims any kind (single-process
    dev driver). Chat kinds still sort first within a lane."""
    # None = no filter; an empty include list means "this lane has no kinds" and
    # must claim NOTHING (not everything), so it is its own short-circuit.
    if include_kinds is not None and not include_kinds:
        return None
    where = ["status='pending'", "run_after <= now()"]
    kind_params: list = []
    if include_kinds:
        where.append("kind = ANY(%s)")
        kind_params.append(list(include_kinds))
    if exclude_kinds:
        where.append("NOT (kind = ANY(%s))")
        kind_params.append(list(exclude_kinds))
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
                WHERE {" AND ".join(where)}
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
            (f"{worker_id}@{socket.gethostname()}", lease_seconds, *kind_params),
        )
        row = cur.fetchone()
        if not row:
            return None
        job = _row_to_job(row)
        log.info("claimed job %s (kind=%s role=%s team=%s request=%s) by %s",
                 job.id, job.kind, job.role, job.team_id, job.request_id, worker_id,
                 extra={"job_id": job.id, "team_id": job.team_id,
                        "request_id": job.request_id})
        return job


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
            log.warning("finalize fenced out for job %s (stale token or reclaimed)", job_id,
                        extra={"job_id": job_id})
            return False  # fenced out: stale token or already reclaimed
        attempt = row[0]
        # DO UPDATE (not DO NOTHING): an outage release for this same attempt
        # may have already inserted a placeholder run row (jobs.release does
        # not bump attempts, so the eventual finalize reuses that attempt
        # number). Without overwriting it here, the REAL final run -- output,
        # cost_usd, status -- would be silently dropped behind the outage
        # marker, corrupting cost accounting and retro learning. This is safe
        # because we only reach this insert after the CAS UPDATE above
        # succeeded, i.e. this call is the current lease holder.
        cur.execute(
            """
            INSERT INTO runs (job_id, attempt, claim_token, role, engine, model,
                              prompt, output, cost_source, cost_usd, status,
                              prompt_hash, ended_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
            ON CONFLICT (job_id, attempt) DO UPDATE SET
                claim_token = EXCLUDED.claim_token,
                role = EXCLUDED.role,
                engine = EXCLUDED.engine,
                model = EXCLUDED.model,
                prompt = EXCLUDED.prompt,
                output = EXCLUDED.output,
                cost_source = EXCLUDED.cost_source,
                cost_usd = EXCLUDED.cost_usd,
                status = EXCLUDED.status,
                prompt_hash = EXCLUDED.prompt_hash,
                ended_at = now()
            """,
            (job_id, attempt, claim_token, run.role, run.engine, run.model,
             run.prompt, run.output, run.cost_source, run.cost_usd, run.status,
             run.prompt_hash),
        )
        cur.execute("SELECT request_id, team_id FROM jobs WHERE id=%s", (job_id,))
        request_id, team_id = cur.fetchone()
        request_id = _s(request_id)
        log.info("finalized job %s status=%s attempt=%s team=%s request=%s prompt_hash=%s",
                 job_id, status, attempt, team_id, request_id, run.prompt_hash,
                 extra={"job_id": str(job_id), "team_id": team_id,
                        "request_id": request_id, "prompt_hash": run.prompt_hash})
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


def release(conn: psycopg.Connection, job_id: str, claim_token: str, *,
            delay_seconds: int, run: RunRecord, reason: str) -> bool:
    """Put a claimed/running job back to pending with a delay, WITHOUT
    incrementing attempts. For engine outages: the failure belongs to the
    engine, not the job, so the job's attempt budget must survive the outage
    (2026-07-02: a codex usage limit permanently failed 14 requests in
    minutes). Bumps payload.outage_releases so callers can cap total releases
    for a job whose "outage" never heals (engine binary gone). CAS-fenced by
    claim_token like finalize; records the run row for observability (the
    (job_id, attempt) unique index keeps the first outage row per attempt)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE jobs SET
                status='pending',
                claim_token=NULL, claimed_by=NULL, claimed_at=NULL,
                lease_expires_at=NULL, heartbeat_at=NULL,
                run_after=now() + make_interval(secs => %s),
                payload = jsonb_set(COALESCE(payload, '{}'::jsonb),
                                    '{outage_releases}',
                                    to_jsonb(COALESCE((payload->>'outage_releases')::int, 0) + 1)),
                updated_at=now()
            WHERE id=%s AND claim_token=%s AND status IN ('claimed','running')
            RETURNING attempts, (payload->>'outage_releases')::int
            """,
            (delay_seconds, job_id, claim_token),
        )
        row = cur.fetchone()
        if not row:
            log.warning("release fenced out for job %s (stale token or reclaimed)",
                        job_id, extra={"job_id": str(job_id)})
            return False
        attempt, releases = row
        cur.execute(
            """
            INSERT INTO runs (job_id, attempt, claim_token, role, engine, model,
                              prompt, output, cost_source, cost_usd, status,
                              prompt_hash, ended_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
            ON CONFLICT (job_id, attempt) DO NOTHING
            """,
            (job_id, attempt, claim_token, run.role, run.engine, run.model,
             run.prompt, run.output, run.cost_source, run.cost_usd, run.status,
             run.prompt_hash),
        )
    log.info("released job %s to pending (reason=%s delay=%ss releases=%s)",
             job_id, reason, delay_seconds, releases,
             extra={"job_id": str(job_id)})
    return True


def list_dead(conn: psycopg.Connection, *, limit: int = 50) -> list[dict]:
    """Dead jobs (exhausted max_attempts), newest first, with a best-effort
    last-error snippet pulled the same way the dead-job alert does: jobs.result
    first, else the most recent run row's output."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT j.id, j.team_id, j.kind, j.attempts, j.max_attempts, j.result,
                   j.updated_at,
                   (SELECT output FROM runs WHERE job_id = j.id
                    ORDER BY started_at DESC LIMIT 1) AS last_run_output
            FROM jobs j WHERE j.status='dead'
            ORDER BY j.updated_at DESC LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    out = []
    for job_id, team_id, kind, attempts, max_attempts, result, died_at, last_run_output in rows:
        detail = ""
        if isinstance(result, dict):
            detail = str(result.get("test_output") or result.get("output")
                         or result.get("error") or "").strip()
        if not detail:
            detail = str(last_run_output or "").strip()
        out.append({
            "id": str(job_id), "team_id": team_id, "kind": kind,
            "attempts": attempts, "max_attempts": max_attempts,
            "last_error": detail, "died_at": died_at,
        })
    return out


def retry_dead(conn: psycopg.Connection, job_id: str) -> bool:
    """Make a dead job claimable again: reset to pending, clear claim/lease
    state, zero attempts, and bump max_attempts by one so it gets a fresh
    attempt budget instead of dying again on its first heartbeat check.
    Returns False if the job does not exist or is not currently dead."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE jobs SET
                status='pending',
                attempts=0,
                max_attempts=max_attempts + 1,
                claim_token=NULL,
                claimed_by=NULL, claimed_at=NULL,
                lease_expires_at=NULL, heartbeat_at=NULL,
                run_after=now(),
                advanced_at=NULL,
                updated_at=now()
            WHERE id=%s AND status='dead'
            """,
            (job_id,),
        )
        ok = cur.rowcount == 1
    if ok:
        log.info("dead job %s reset to pending for retry", job_id)
    return ok


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
            RETURNING id, request_id, team_id, status
            """,
        )
        rows = cur.fetchall()
        n = len(rows)
        if n:
            log.warning("reclaimed %d expired job(s) (worker crash or lease timeout)", n)
            for job_id, request_id, team_id, status in rows:
                if status == "dead":
                    log.warning(
                        "job %s went dead (max attempts exhausted) team=%s request=%s",
                        job_id, team_id, request_id,
                        extra={"job_id": str(job_id), "team_id": team_id,
                               "request_id": _s(request_id)})
        return n
