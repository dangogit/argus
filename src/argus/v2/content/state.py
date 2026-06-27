"""Postgres-backed content draft and queue state."""
from __future__ import annotations

import hashlib
import os
import random
from datetime import datetime, timezone
from pathlib import Path

from argus.v2 import alerts
from argus.v2.db import pool


def content_dir() -> Path:
    if os.environ.get("ARGUS_CONTENT_DIR"):
        return Path(os.path.expandvars(os.environ["ARGUS_CONTENT_DIR"])).expanduser()
    artifact_root = os.environ.get("ARGUS_ARTIFACT_DIR", str(Path.cwd() / "run"))
    return Path(os.path.expandvars(artifact_root)).expanduser() / "content"


def _content_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{random.randint(0, 99999)}"


def register(project: str, platform: str) -> str:
    draft_id = _content_id()
    (content_dir() / draft_id).mkdir(parents=True, exist_ok=True)
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO content_drafts (id, project, platform, status)
                VALUES (%s,%s,%s,'ready')
                """,
                (draft_id, project, platform),
            )
        conn.commit()
    return draft_id


def latest_draft(draft_id: str) -> dict | None:
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, created_at, project, platform, status
                FROM content_drafts
                WHERE id=%s
                """,
                (draft_id,),
            )
            row = cur.fetchone()
    return _draft_row(row) if row else None


def set_draft_status(draft_id: str, status: str) -> None:
    if status not in {"ready", "published", "cleared", "failed"}:
        raise ValueError(f"invalid draft status: {status}")
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE content_drafts
                SET status=%s, updated_at=clock_timestamp()
                WHERE id=%s
                """,
                (status, draft_id),
            )
            if cur.rowcount == 0:
                raise ValueError(f"unknown content draft: {draft_id}")
        conn.commit()


def pending() -> list[dict]:
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, created_at, project, platform, status
                FROM content_drafts
                WHERE status='ready'
                ORDER BY created_at DESC
                """
            )
            rows = cur.fetchall()
    return [_draft_row(row) for row in rows]


def draft_list(status: str | None = None, *, limit: int = 50) -> list[dict]:
    clauses = []
    params: list[object] = []
    if status:
        clauses.append("status=%s")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, created_at, project, platform, status
                FROM content_drafts
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()
    return [_draft_row(row) for row in rows]


def queue_add(project: str, platform: str, request: str) -> str:
    queue_id = _content_id()
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO content_queue (id, project, platform, request, status, attempts)
                VALUES (%s,%s,%s,%s,'queued',0)
                """,
                (queue_id, project, platform, request),
            )
        conn.commit()
    return queue_id


def queue_latest(queue_id: str) -> dict | None:
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, created_at, project, platform, request, status, attempts
                FROM content_queue
                WHERE id=%s
                """,
                (queue_id,),
            )
            row = cur.fetchone()
    return _queue_row(row) if row else None


def queue_list(status: str | None = None, *, limit: int = 50) -> list[dict]:
    clauses = []
    params: list[object] = []
    if status:
        clauses.append("status=%s")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, created_at, project, platform, request, status, attempts
                FROM content_queue
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()
    return [_queue_row(row) for row in rows]


def queue_oldest() -> dict | None:
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, created_at, project, platform, request, status, attempts
                FROM content_queue
                WHERE status='queued'
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """
            )
            row = cur.fetchone()
    return _queue_row(row) if row else None


def queue_set_status(queue_id: str, status: str, attempts: int | None = None) -> None:
    if status not in {"queued", "drafted", "dead"}:
        raise ValueError(f"invalid queue status: {status}")
    with pool.connect() as conn:
        with conn.cursor() as cur:
            if attempts is None:
                cur.execute(
                    """
                    UPDATE content_queue
                    SET status=%s, updated_at=clock_timestamp()
                    WHERE id=%s
                    """,
                    (status, queue_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE content_queue
                    SET status=%s, attempts=%s, updated_at=clock_timestamp()
                    WHERE id=%s
                    """,
                    (status, int(attempts), queue_id),
                )
            if cur.rowcount == 0:
                raise ValueError(f"unknown queue entry: {queue_id}")
        conn.commit()


def breaker_check(project: str, cap: int) -> None:
    if cap < 0:
        raise ValueError(f"invalid cap: {cap}")
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*)
                FROM content_breaker_events
                WHERE project=%s AND breaker_day=current_date
                """,
                (project,),
            )
            count = int(cur.fetchone()[0])
    if count >= cap:
        raise RuntimeError(f"content breaker: {project} reached the daily cap ({count}/{cap})")


def breaker_record(project: str) -> None:
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO content_breaker_events (project, breaker_day) VALUES (%s,current_date)",
                (project,),
            )
        conn.commit()


def alert_warn(fingerprint: str, message: str) -> None:
    alerts.emit(
        severity="warn",
        project="content",
        fingerprint=fingerprint,
        message=message,
        channel="log",
    )


def stable_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _ts(value) -> str:
    if hasattr(value, "astimezone"):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value or "")


def _draft_row(row) -> dict:
    draft_id, created_at, project, platform, status = row
    return {
        "id": str(draft_id),
        "ts": _ts(created_at),
        "project": project,
        "platform": platform,
        "status": status,
    }


def _queue_row(row) -> dict:
    queue_id, created_at, project, platform, request, status, attempts = row
    return {
        "id": str(queue_id),
        "ts": _ts(created_at),
        "project": project,
        "platform": platform,
        "request": request,
        "status": status,
        "attempts": int(attempts or 0),
    }
