"""Postgres-backed advisor state for public WhatsApp group jobs."""
from __future__ import annotations

import hashlib
from datetime import date, timezone

from psycopg.types.json import Json

from argus.v2.db import pool


def record_message(jid: str, row: dict) -> str:
    message_id = str(row.get("id") or row.get("message_id") or _message_key(jid, row))
    mentioned = row.get("mentioned") if isinstance(row.get("mentioned"), list) else []
    with pool.connect() as conn:
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
                    _int(row.get("ts")),
                    str(row.get("participant") or ""),
                    str(row.get("participant_jid") or ""),
                    str(row.get("push_name") or ""),
                    str(row.get("body") or ""),
                    Json(mentioned),
                    str(row.get("quoted_participant") or ""),
                    Json(row),
                ),
            )
        conn.commit()
    return message_id


def messages(jid: str) -> list[dict]:
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT message_id, ts, participant, participant_jid, push_name,
                       body, mentioned, quoted_participant, payload
                FROM advisor_messages
                WHERE jid=%s
                ORDER BY seq ASC
                """,
                (jid,),
            )
            rows = cur.fetchall()
    return [_message_row(row) for row in rows]


def replies(jid: str) -> list[dict]:
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ts, participant, reply_to_id, parts, skipped, reason, payload
                FROM advisor_replies
                WHERE jid=%s
                ORDER BY created_at ASC, id ASC
                """,
                (jid,),
            )
            rows = cur.fetchall()
    return [_reply_row(row) for row in rows]


def record_reply(jid: str, row: dict) -> None:
    with pool.connect() as conn:
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
                    _int(row.get("ts")),
                    str(row.get("participant") or ""),
                    str(row.get("reply_to_id") or ""),
                    _int(row.get("parts")),
                    bool(row.get("skipped") is True),
                    str(row.get("reason") or ""),
                    Json(row),
                ),
            )
        conn.commit()


def cursor(jid: str) -> int:
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM advisor_cursors WHERE jid=%s", (jid,))
            row = cur.fetchone()
    return int(row[0]) if row else 0


def set_cursor(jid: str, value: int) -> None:
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO advisor_cursors (jid, value, updated_at)
                VALUES (%s,%s,clock_timestamp())
                ON CONFLICT (jid) DO UPDATE
                SET value=EXCLUDED.value,
                    updated_at=clock_timestamp()
                """,
                (jid, int(value)),
            )
        conn.commit()


def replies_last_hour(jid: str, now: int, participant: str | None = None) -> int:
    low = now - 3600
    with pool.connect() as conn:
        with conn.cursor() as cur:
            if participant:
                cur.execute(
                    """
                    SELECT count(*)
                    FROM advisor_replies
                    WHERE jid=%s AND ts >= %s AND skipped=false AND participant=%s
                    """,
                    (jid, low, participant),
                )
            else:
                cur.execute(
                    """
                    SELECT count(*)
                    FROM advisor_replies
                    WHERE jid=%s AND ts >= %s AND skipped=false
                    """,
                    (jid, low),
                )
            return int(cur.fetchone()[0])


def active_conversation(jid: str, now: int, participant: str, window_min: int) -> bool:
    if not participant:
        return False
    low = now - (window_min * 60)
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM advisor_replies
                WHERE jid=%s AND participant=%s AND ts >= %s AND skipped=false
                LIMIT 1
                """,
                (jid, participant, low),
            )
            return cur.fetchone() is not None


def attempts(jid: str, message_id: str) -> int:
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT attempts FROM advisor_attempts WHERE jid=%s AND message_id=%s",
                (jid, message_id),
            )
            row = cur.fetchone()
    return int(row[0]) if row else 0


def bump_attempts(jid: str, message_id: str) -> int:
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO advisor_attempts (jid, message_id, attempts, updated_at)
                VALUES (%s,%s,1,clock_timestamp())
                ON CONFLICT (jid, message_id) DO UPDATE
                SET attempts=advisor_attempts.attempts + 1,
                    updated_at=clock_timestamp()
                RETURNING attempts
                """,
                (jid, message_id),
            )
            value = int(cur.fetchone()[0])
        conn.commit()
    return value


def clear_attempts(jid: str, message_id: str) -> None:
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM advisor_attempts WHERE jid=%s AND message_id=%s",
                (jid, message_id),
            )
        conn.commit()


def digests(jid: str) -> list[dict]:
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT digest_date, message_count, posted, seed_topic, reason, payload,
                       created_at
                FROM advisor_digests
                WHERE jid=%s
                ORDER BY digest_date ASC, created_at ASC
                """,
                (jid,),
            )
            rows = cur.fetchall()
    return [_digest_row(row) for row in rows]


def digest_seen(jid: str, digest_date: str) -> bool:
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM advisor_digests WHERE jid=%s AND digest_date=%s",
                (jid, digest_date),
            )
            return cur.fetchone() is not None


def record_digest(jid: str, row: dict) -> None:
    digest_date = str(row.get("date") or "")
    if not digest_date:
        raise ValueError("advisor digest date required")
    with pool.connect() as conn:
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
                    _int(row.get("message_count")),
                    bool(row.get("posted") is True),
                    str(row.get("seed_topic") or ""),
                    str(row.get("reason") or ""),
                    Json(row),
                ),
            )
        conn.commit()


def groups() -> list[str]:
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT jid FROM advisor_messages
                UNION SELECT jid FROM advisor_replies
                UNION SELECT jid FROM advisor_cursors
                UNION SELECT jid FROM advisor_digests
                ORDER BY jid
                """
            )
            return [str(row[0]) for row in cur.fetchall()]


def status_rows() -> list[dict]:
    out: list[dict] = []
    for jid in groups():
        with pool.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM advisor_messages WHERE jid=%s", (jid,))
                message_count = int(cur.fetchone()[0])
                cur.execute("SELECT count(*) FROM advisor_replies WHERE jid=%s", (jid,))
                reply_count = int(cur.fetchone()[0])
                cur.execute("SELECT count(*) FROM advisor_digests WHERE jid=%s", (jid,))
                digest_count = int(cur.fetchone()[0])
        out.append({
            "jid": jid,
            "cursor": cursor(jid),
            "messages": message_count,
            "replies": reply_count,
            "digests": digest_count,
        })
    return out


def _message_row(row) -> dict:
    message_id, ts, participant, participant_jid, push_name, body, mentioned, quoted, payload = row
    value = dict(payload or {})
    value.update({
        "id": message_id,
        "ts": int(ts or 0),
        "participant": participant,
        "participant_jid": participant_jid,
        "push_name": push_name,
        "body": body,
        "mentioned": mentioned if isinstance(mentioned, list) else [],
        "quoted_participant": quoted,
    })
    return value


def _reply_row(row) -> dict:
    ts, participant, reply_to_id, parts, skipped, reason, payload = row
    value = dict(payload or {})
    value.update({
        "ts": int(ts or 0),
        "participant": participant,
        "reply_to_id": reply_to_id,
        "parts": int(parts or 0),
        "skipped": bool(skipped),
        "reason": reason,
    })
    if not value["skipped"]:
        value.pop("skipped", None)
    if not value["reason"]:
        value.pop("reason", None)
    return value


def _digest_row(row) -> dict:
    digest_date, message_count, posted, seed_topic, reason, payload, created_at = row
    value = dict(payload or {})
    value.update({
        "date": _date_str(digest_date),
        "message_count": int(message_count or 0),
        "posted": bool(posted),
        "seed_topic": seed_topic,
        "reason": reason,
        "created_at": _ts(created_at),
    })
    if not value["seed_topic"]:
        value.pop("seed_topic", None)
    if not value["reason"]:
        value.pop("reason", None)
    return value


def _message_key(jid: str, row: dict) -> str:
    parts = [
        jid,
        str(row.get("ts") or ""),
        str(row.get("participant") or ""),
        str(row.get("body") or ""),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _date_str(value) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "")


def _ts(value) -> str:
    if hasattr(value, "astimezone"):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value or "")
