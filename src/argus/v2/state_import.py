"""One-time importer from legacy run-root state into v2 Postgres tables."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Json

from argus.v2 import alerts
from argus.v2.context import state as context_state


def run(conn: psycopg.Connection, *, run_root: Path, dry_run: bool = False) -> dict[str, int]:
    counts = {
        "alerts": import_alerts(conn, run_root / "alerts.jsonl"),
        "advisor": import_advisor(conn, run_root / "advisor"),
        "content": import_content(conn, run_root / "content"),
        "context": import_context(conn, run_root / "context"),
        "support": import_support(conn, run_root / "support"),
        "assistant": import_assistant(conn, run_root / "assistant"),
    }
    if dry_run:
        conn.rollback()
    return counts


def import_alerts(conn: psycopg.Connection, path: Path) -> int:
    count = 0
    for row in _read_jsonl(path):
        severity = str(row.get("severity") or "info")
        if severity not in alerts.SEVERITIES:
            severity = "info"
        project = str(row.get("project") or "legacy")
        fingerprint = str(row.get("fingerprint") or _fingerprint(row))
        message = str(row.get("message") or row.get("title") or "")
        channel = str(row.get("channel") or alerts.channel_for_severity(severity))
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO alerts (ts, severity, project, fingerprint, message, channel, payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (_parse_ts(row.get("ts")), severity, project, fingerprint, message, channel, Json(row)),
            )
        count += 1
    return count


def import_advisor(conn: psycopg.Connection, root: Path) -> int:
    if not root.exists():
        return 0
    count = 0
    for group_dir in [p for p in root.iterdir() if p.is_dir()]:
        jid = _advisor_jid(group_dir.name)
        for row in _read_jsonl(group_dir / "messages.jsonl"):
            message_id = str(row.get("id") or _fingerprint(row))
            mentioned = row.get("mentioned") if isinstance(row.get("mentioned"), list) else []
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO advisor_messages (
                      jid, message_id, ts, participant, participant_jid, push_name,
                      body, mentioned, quoted_participant, payload
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (jid, message_id) DO UPDATE
                    SET ts=EXCLUDED.ts,
                        participant=EXCLUDED.participant,
                        participant_jid=EXCLUDED.participant_jid,
                        push_name=EXCLUDED.push_name,
                        body=EXCLUDED.body,
                        mentioned=EXCLUDED.mentioned,
                        quoted_participant=EXCLUDED.quoted_participant,
                        payload=EXCLUDED.payload
                    """,
                    (
                        jid,
                        message_id,
                        int(row.get("ts") or 0),
                        str(row.get("participant") or ""),
                        str(row.get("participant_jid") or ""),
                        str(row.get("push_name") or ""),
                        str(row.get("body") or ""),
                        Json(mentioned),
                        str(row.get("quoted_participant") or ""),
                        Json(row),
                    ),
                )
            count += 1
        for row in _read_jsonl(group_dir / "replies.jsonl"):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO advisor_replies (
                      jid, ts, participant, reply_to_id, parts, skipped, reason, payload
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        jid,
                        int(row.get("ts") or 0),
                        str(row.get("participant") or ""),
                        str(row.get("reply_to_id") or ""),
                        int(row.get("parts") or 0),
                        bool(row.get("skipped") is True),
                        str(row.get("reason") or ""),
                        Json(row),
                    ),
                )
            count += 1
        for row in _read_jsonl(group_dir / "digests.jsonl"):
            digest_date = str(row.get("date") or "")
            if not digest_date:
                continue
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO advisor_digests (
                      jid, digest_date, message_count, posted, seed_topic, reason, payload
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (jid, digest_date) DO NOTHING
                    """,
                    (
                        jid,
                        digest_date,
                        int(row.get("message_count") or 0),
                        bool(row.get("posted") is True),
                        str(row.get("seed_topic") or ""),
                        str(row.get("reason") or ""),
                        Json(row),
                    ),
                )
            count += 1
        cursor = _read_int(group_dir / "cursor")
        if cursor:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO advisor_cursors (jid, value, updated_at)
                    VALUES (%s,%s,clock_timestamp())
                    ON CONFLICT (jid) DO UPDATE
                    SET value=EXCLUDED.value,
                        updated_at=clock_timestamp()
                    """,
                    (jid, cursor),
                )
            count += 1
        attempts_dir = group_dir / "attempts"
        if attempts_dir.exists():
            for item in [p for p in attempts_dir.iterdir() if p.is_file()]:
                attempts = _read_int(item)
                if attempts <= 0:
                    continue
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO advisor_attempts (jid, message_id, attempts, updated_at)
                        VALUES (%s,%s,%s,clock_timestamp())
                        ON CONFLICT (jid, message_id) DO UPDATE
                        SET attempts=EXCLUDED.attempts,
                            updated_at=clock_timestamp()
                        """,
                        (jid, item.name, attempts),
                    )
                count += 1
    return count


def import_content(conn: psycopg.Connection, root: Path) -> int:
    count = 0
    for row in _read_jsonl(root / "index.jsonl"):
        draft_id = str(row.get("id") or "")
        if not draft_id:
            continue
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO content_drafts (id, project, platform, status, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE
                SET project=EXCLUDED.project,
                    platform=EXCLUDED.platform,
                    status=EXCLUDED.status,
                    updated_at=EXCLUDED.updated_at
                """,
                (
                    draft_id,
                    str(row.get("project") or ""),
                    str(row.get("platform") or ""),
                    str(row.get("status") or "ready"),
                    _parse_ts(row.get("ts")),
                    _parse_ts(row.get("ts")),
                ),
            )
        count += 1
    for row in _read_jsonl(root / "queue.jsonl"):
        queue_id = str(row.get("id") or "")
        if not queue_id:
            continue
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO content_queue
                  (id, project, platform, request, status, attempts, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE
                SET project=EXCLUDED.project,
                    platform=EXCLUDED.platform,
                    request=EXCLUDED.request,
                    status=EXCLUDED.status,
                    attempts=EXCLUDED.attempts,
                    updated_at=EXCLUDED.updated_at
                """,
                (
                    queue_id,
                    str(row.get("project") or ""),
                    str(row.get("platform") or ""),
                    str(row.get("request") or ""),
                    str(row.get("status") or "queued"),
                    int(row.get("attempts") or 0),
                    _parse_ts(row.get("ts")),
                    _parse_ts(row.get("ts")),
                ),
            )
        count += 1
    for row in _read_jsonl(root / "breaker.jsonl"):
        project = str(row.get("project") or "")
        if not project:
            continue
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO content_breaker_events (project, breaker_day) VALUES (%s,%s)",
                (project, str(row.get("date") or datetime.now(timezone.utc).date())),
            )
        count += 1
    return count


def import_context(conn: psycopg.Connection, root: Path) -> int:
    count = 0
    for row in _read_jsonl(root / "watermarks.jsonl"):
        job = str(row.get("job") or "")
        if not job:
            continue
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO context_watermarks (job, source, last_id, updated_at)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (job) DO UPDATE
                SET source=EXCLUDED.source,
                    last_id=EXCLUDED.last_id,
                    updated_at=EXCLUDED.updated_at
                """,
                (
                    job,
                    str(row.get("source") or ""),
                    int(row.get("last_id") or 0),
                    _parse_ts(row.get("ts")),
                ),
            )
        count += 1
    for row in _read_jsonl(root / "commitments.jsonl"):
        commit_id = str(row.get("id") or "")
        what = str(row.get("what") or "")
        if not commit_id or not what:
            continue
        digest = str(row.get("dedup_hash") or context_state.dedup_hash(
            what,
            str(row.get("due_at") or ""),
            str(row.get("source_ref") or ""),
        ))
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO context_commitments
                  (id, dedup_hash, who, what, due_at, source_ref, status,
                   surfaced_at, snooze_until, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE
                SET status=EXCLUDED.status,
                    surfaced_at=EXCLUDED.surfaced_at,
                    snooze_until=EXCLUDED.snooze_until,
                    updated_at=EXCLUDED.updated_at
                """,
                (
                    commit_id,
                    digest,
                    str(row.get("who") or ""),
                    what,
                    str(row.get("due_at") or ""),
                    str(row.get("source_ref") or ""),
                    str(row.get("status") or "open"),
                    str(row.get("surfaced_at") or ""),
                    str(row.get("snooze_until") or ""),
                    _parse_ts(row.get("ts")),
                    _parse_ts(row.get("ts")),
                ),
            )
        count += 1
    for row in _read_jsonl(root / "reminder-batches.jsonl"):
        fingerprint = str(row.get("fingerprint") or "")
        if not fingerprint:
            continue
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO context_reminder_batches (fingerprint, created_at)
                VALUES (%s,%s)
                ON CONFLICT (fingerprint) DO NOTHING
                """,
                (fingerprint, _parse_ts(row.get("ts"))),
            )
        count += 1
    return count


def import_support(conn: psycopg.Connection, root: Path) -> int:
    if not root.exists():
        return 0
    count = 0
    for project_dir in [p for p in root.iterdir() if p.is_dir()]:
        project = project_dir.name
        count += _import_support_threads(conn, project, project_dir / "threads.jsonl")
        count += _import_support_drafts(conn, project, project_dir / "drafts")
        count += _import_support_guidance(conn, project, project_dir / "guidance")
    return count


def import_assistant(conn: psycopg.Connection, root: Path) -> int:
    count = 0
    for row in _read_jsonl(root / "history.jsonl"):
        role = str(row.get("role") or "unknown")
        text = str(row.get("text") or "")
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO assistant_history (role, text, payload, created_at)
                VALUES (%s,%s,%s,%s)
                """,
                (role, text, Json(row), _parse_ts(row.get("ts"))),
            )
        count += 1
    memory_file = root / "memory.md"
    if memory_file.exists():
        watermark = _read_int(root / "memory.watermark")
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO assistant_memory (name, content, watermark, updated_at)
                VALUES ('default', %s, %s, clock_timestamp())
                ON CONFLICT (name) DO UPDATE
                SET content=EXCLUDED.content,
                    watermark=EXCLUDED.watermark,
                    updated_at=clock_timestamp()
                """,
                (memory_file.read_text(encoding="utf-8"), watermark),
            )
        count += 1
    return count


def _import_support_threads(conn: psycopg.Connection, project: str, path: Path) -> int:
    count = 0
    for row in _read_jsonl(path):
        thread_id = str(row.get("thread_id") or "")
        action = str(row.get("action") or "")
        if not thread_id or not action:
            continue
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO support_thread_events
                  (event_key, project, thread_id, action, sender, subject, reason, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (event_key) DO NOTHING
                """,
                (
                    _fingerprint(row),
                    project,
                    thread_id,
                    action,
                    str(row.get("from") or ""),
                    str(row.get("subject") or ""),
                    str(row.get("reason") or ""),
                    _parse_ts(row.get("ts")),
                ),
            )
        count += 1
    return count


def _import_support_drafts(conn: psycopg.Connection, project: str, root: Path) -> int:
    count = 0
    for row in _read_jsonl(root / "index.jsonl"):
        draft_id = str(row.get("id") or "")
        if not draft_id:
            continue
        draft_dir = root / draft_id
        meta = _read_json(draft_dir / "email.json")
        reply = _read_text(draft_dir / "reply.txt")
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO support_drafts
                  (id, project, thread_id, sender, subject, reply, transport,
                   status, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE
                SET status=EXCLUDED.status,
                    reply=EXCLUDED.reply,
                    updated_at=EXCLUDED.updated_at
                """,
                (
                    draft_id,
                    project,
                    str(row.get("thread_id") or meta.get("thread_id") or ""),
                    str(row.get("from") or meta.get("from") or ""),
                    str(row.get("subject") or meta.get("subject") or ""),
                    reply,
                    str(meta.get("transport") or ""),
                    str(row.get("status") or "ready"),
                    _parse_ts(row.get("ts")),
                    _parse_ts(row.get("ts")),
                ),
            )
        count += 1
    return count


def _import_support_guidance(conn: psycopg.Connection, project: str, root: Path) -> int:
    if not root.exists():
        return 0
    latest = {}
    for row in _read_jsonl(root / "index.jsonl"):
        gid = str(row.get("id") or "")
        if gid:
            latest[gid] = row
    count = 0
    for item in [p for p in root.iterdir() if p.is_dir()]:
        gid = item.name
        req = _read_json(item / "request.json")
        if not req:
            continue
        row = latest.get(gid, {})
        answer = _read_json(item / "answer.json")
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO support_guidance
                  (id, project, thread_id, sender, subject, question,
                   proposed_reply, thread_text, status, answer, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE
                SET status=EXCLUDED.status,
                    answer=EXCLUDED.answer,
                    updated_at=EXCLUDED.updated_at
                """,
                (
                    gid,
                    project,
                    str(req.get("thread_id") or ""),
                    str(req.get("from") or ""),
                    str(req.get("subject") or ""),
                    str(req.get("question") or ""),
                    str(req.get("proposed_reply") or ""),
                    str(req.get("thread") or ""),
                    str(row.get("status") or req.get("status") or "pending"),
                    str(row.get("answer") or answer.get("answer") or ""),
                    _parse_ts(req.get("ts")),
                    _parse_ts(row.get("ts")),
                ),
            )
        count += 1
    return count


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _read_int(path: Path) -> int:
    if not path.exists():
        return 0
    raw = path.read_text(encoding="utf-8").strip()
    return int(raw) if raw.isdigit() else 0


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc)
    if isinstance(value, str) and value:
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return datetime.now(timezone.utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _fingerprint(row: dict) -> str:
    payload = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _advisor_jid(name: str) -> str:
    if name.endswith("@g.us"):
        return name
    if name.endswith(".g.us"):
        return name[:-5] + "@g.us"
    return f"{name}@g.us"
