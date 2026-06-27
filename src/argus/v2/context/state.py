"""Append-only context state compatible with the legacy context jobs."""
from __future__ import annotations

import random
import re
import hashlib
from datetime import datetime, timezone

from argus.v2.db import pool


def _context_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{random.randint(0, 99999)}"


def get_watermark(job: str) -> int:
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT last_id FROM context_watermarks WHERE job=%s", (job,))
            row = cur.fetchone()
    return int(row[0]) if row else 0


def set_watermark(job: str, source: str, last_id: int) -> None:
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO context_watermarks (job, source, last_id, updated_at)
                VALUES (%s,%s,%s,clock_timestamp())
                ON CONFLICT (job) DO UPDATE
                SET source=EXCLUDED.source,
                    last_id=EXCLUDED.last_id,
                    updated_at=clock_timestamp()
                """,
                (job, source, int(last_id)),
            )
        conn.commit()


def dedup_hash(what: str, due_at: str, source_ref: str) -> str:
    payload = f"{(what or '').strip()}|{due_at or ''}|{source_ref or ''}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def commit_upsert(who: str, what: str, due_at: str, source_ref: str) -> str:
    what = re.sub(r"^\s+|\s+$", "", what or "")
    if not what:
        raise ValueError("upsert: what required")
    digest = dedup_hash(what, due_at, source_ref)
    row_id = _context_id()
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO context_commitments
                  (id, dedup_hash, who, what, due_at, source_ref, status)
                VALUES (%s,%s,%s,%s,%s,%s,'open')
                ON CONFLICT (dedup_hash) DO NOTHING
                RETURNING id
                """,
                (row_id, digest, who or "", what, due_at or "", source_ref or ""),
            )
            inserted = cur.fetchone()
            if inserted:
                conn.commit()
                return str(inserted[0])
            cur.execute("SELECT id FROM context_commitments WHERE dedup_hash=%s", (digest,))
            existing = cur.fetchone()
        conn.commit()
    return str(existing[0])


def _commit_latest(commit_id: str) -> dict | None:
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, dedup_hash, who, what, due_at, source_ref, status,
                       surfaced_at, snooze_until, updated_at
                FROM context_commitments
                WHERE id=%s
                """,
                (commit_id,),
            )
            row = cur.fetchone()
    return _commit_row(row) if row else None


def commit_set(commit_id: str, field: str, value: str) -> None:
    if field == "status" and value not in {"open", "surfaced", "done", "dismissed"}:
        raise ValueError(f"invalid status: {value}")
    if field not in {"status", "surfaced_at", "snooze_until"}:
        raise ValueError(f"invalid commitment field: {field}")
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE context_commitments
                SET {field}=%s, updated_at=clock_timestamp()
                WHERE id=%s
                """,
                (value, commit_id),
            )
            if cur.rowcount == 0:
                raise ValueError(f"unknown commitment: {commit_id}")
        conn.commit()


def commit_list(status: str) -> list[dict]:
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, dedup_hash, who, what, due_at, source_ref, status,
                       surfaced_at, snooze_until, updated_at
                FROM context_commitments
                WHERE status=%s
                ORDER BY due_at ASC, id ASC
                """,
                (status,),
            )
            rows = cur.fetchall()
    return [_commit_row(row) for row in rows]


def batch_seen(fingerprint: str) -> bool:
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM context_reminder_batches WHERE fingerprint=%s",
                (fingerprint,),
            )
            return cur.fetchone() is not None


def batch_record(fingerprint: str) -> None:
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO context_reminder_batches (fingerprint)
                VALUES (%s)
                ON CONFLICT (fingerprint) DO NOTHING
                """,
                (fingerprint,),
            )
        conn.commit()


def _ts(value) -> str:
    if hasattr(value, "astimezone"):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value or "")


def _commit_row(row) -> dict:
    commit_id, digest, who, what, due_at, source_ref, status, surfaced_at, snooze_until, updated_at = row
    return {
        "id": str(commit_id),
        "dedup_hash": digest,
        "who": who,
        "what": what,
        "due_at": due_at,
        "source_ref": source_ref,
        "status": status,
        "surfaced_at": surfaced_at,
        "snooze_until": snooze_until,
        "ts": _ts(updated_at),
    }
