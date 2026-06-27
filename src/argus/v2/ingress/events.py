"""Ingest a message or signal into the durable inbox. Message redelivery and
signal re-polls collapse on (source, dedup_key). Media blobs are written
atomically before the row is committed by the caller's transaction."""
from __future__ import annotations

from typing import Iterable, Optional

import psycopg
from psycopg.types.json import Json

from argus.v2.ingress import media


def _existing(conn: psycopg.Connection, source: str, dedup_key: str) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM events WHERE source=%s AND dedup_key=%s",
                    (source, dedup_key))
        row = cur.fetchone()
    return str(row[0]) if row else None


def ingest_message(conn: psycopg.Connection, cfg, *, team: str, source: str,
                   dedup_key: str, text: str, media: Iterable[dict] = (),
                   conversation_key: Optional[str] = None,
                   metadata: Optional[dict] = None) -> str:
    existing = _existing(conn, source, dedup_key)
    if existing:
        return existing
    with conn.cursor() as cur:
        if conversation_key is not None:
            cur.execute("SELECT id FROM conversations WHERE team_id=%s AND channel_ref=%s",
                        (team, conversation_key))
            row = cur.fetchone()
            if row:
                conv_id = str(row[0])
            else:
                cur.execute("INSERT INTO conversations (team_id, channel_ref) VALUES (%s,%s) "
                            "RETURNING id", (team, conversation_key))
                conv_id = str(cur.fetchone()[0])
        else:
            cur.execute(
                "INSERT INTO conversations (team_id, channel_ref) VALUES (%s,%s) RETURNING id",
                (team, f"{source}:{dedup_key}"))
            conv_id = str(cur.fetchone()[0])
        cur.execute(
            """INSERT INTO events (conversation_id, team_id, kind, source, payload, dedup_key)
               VALUES (%s,%s,'message',%s,%s,%s)
               ON CONFLICT (source, dedup_key) DO NOTHING RETURNING id""",
            (conv_id, team, source, Json({**(metadata or {}), "text": text}), dedup_key))
        row = cur.fetchone()
        if not row:  # raced; reuse the existing event
            return _existing(conn, source, dedup_key)
        event_id = str(row[0])
    _attach_media(conn, event_id, media)
    return event_id


def ingest_signal(conn: psycopg.Connection, cfg, *, team: str, source: str,
                  fingerprint: str, payload: dict) -> str:
    existing = _existing(conn, source, fingerprint)
    if existing:
        return existing
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO events (team_id, kind, source, payload, dedup_key)
               VALUES (%s,'signal',%s,%s,%s)
               ON CONFLICT (source, dedup_key) DO NOTHING RETURNING id""",
            (team, source, Json(payload), fingerprint))
        row = cur.fetchone()
        if not row:
            return _existing(conn, source, fingerprint)
        return str(row[0])


def _attach_media(conn: psycopg.Connection, event_id: str, items: Iterable[dict]) -> None:
    for m in items:
        path, nbytes, checksum = media.store_blob(m["src"], event_id=event_id, kind=m["kind"])
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO media (event_id, kind, path, mime, bytes, checksum) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (event_id, m["kind"], path, m.get("mime"), nbytes, checksum))


def claim_unprocessed(conn: psycopg.Connection, limit: int = 20) -> list:
    """Return received events and mark them 'processing' (claimed for the front /
    signal router). SKIP LOCKED so multiple orchestrators don't double-handle."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE events SET status='processing'
               WHERE id IN (SELECT id FROM events WHERE status='received'
                            ORDER BY received_at FOR UPDATE SKIP LOCKED LIMIT %s)
               RETURNING id, team_id, kind, conversation_id, dedup_key, payload""",
            (limit,))
        return cur.fetchall()
