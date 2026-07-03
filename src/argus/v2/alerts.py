"""Postgres-backed v2 alert store."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Json

from argus.v2.db import pool

SEVERITIES = {"info", "warn", "error", "critical"}
CHANNELS = {"log", "whatsapp"}
_OWNER_UPDATED_AT_DEDUP_SECONDS = 300


@dataclass(frozen=True)
class Alert:
    id: str
    ts: datetime
    severity: str
    project: str
    fingerprint: str
    message: str
    channel: str
    payload: dict[str, Any]


def channel_for_severity(severity: str) -> str:
    severity = _severity(severity)
    if severity in {"error", "critical"}:
        return "whatsapp"
    return "log"


def record(
    conn: psycopg.Connection,
    *,
    severity: str,
    project: str,
    fingerprint: str,
    message: str,
    channel: str | None = None,
    payload: dict[str, Any] | None = None,
    cooldown_seconds: int = 0,
) -> str | None:
    severity = _severity(severity)
    resolved_channel = _channel(channel or channel_for_severity(severity))
    payload_data = payload or {}
    updated_at = _payload_updated_at(payload_data)
    if resolved_channel == "whatsapp" and updated_at:
        _lock_owner_alert(conn, project, fingerprint, resolved_channel, updated_at)
        if _recent_alert_updated_at(
            conn,
            project,
            fingerprint,
            resolved_channel,
            updated_at,
            _OWNER_UPDATED_AT_DEDUP_SECONDS,
        ):
            return None
    if cooldown_seconds > 0 and _recent_alert(
        conn, project, fingerprint, resolved_channel, cooldown_seconds
    ):
        return None  # same-fingerprint alert still inside the cooldown window
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO alerts (severity, project, fingerprint, message, channel, payload)
            VALUES (%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                severity,
                str(project),
                str(fingerprint),
                str(message),
                resolved_channel,
                Json(payload_data),
            ),
        )
        return str(cur.fetchone()[0])


def _payload_updated_at(payload: dict[str, Any]) -> str:
    value = payload.get("updated_at")
    return str(value).strip() if value is not None else ""


def _lock_owner_alert(
    conn: psycopg.Connection,
    project: str,
    fingerprint: str,
    channel: str,
    updated_at: str,
) -> None:
    key = f"{project}\0{fingerprint}\0{channel}\0{updated_at}"
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s), 0)", (key,))


def _recent_alert_updated_at(
    conn: psycopg.Connection,
    project: str,
    fingerprint: str,
    channel: str,
    updated_at: str,
    window_seconds: int,
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM alerts
            WHERE project=%s AND fingerprint=%s AND channel=%s
              AND payload->>'updated_at' = %s
              AND ts > now() - make_interval(secs => %s)
            LIMIT 1
            """,
            (
                str(project),
                str(fingerprint),
                channel,
                updated_at,
                int(window_seconds),
            ),
        )
        return cur.fetchone() is not None


def _recent_alert(
    conn: psycopg.Connection,
    project: str,
    fingerprint: str,
    channel: str,
    cooldown_seconds: int,
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM alerts
            WHERE project=%s AND fingerprint=%s AND channel=%s
              AND ts > now() - make_interval(secs => %s)
            LIMIT 1
            """,
            (str(project), str(fingerprint), channel, int(cooldown_seconds)),
        )
        return cur.fetchone() is not None


def emit(
    *,
    severity: str,
    project: str,
    fingerprint: str,
    message: str,
    channel: str | None = None,
    payload: dict[str, Any] | None = None,
    cooldown_seconds: int = 0,
) -> str | None:
    try:
        with pool.connect() as conn:
            alert_id = record(
                conn,
                severity=severity,
                project=project,
                fingerprint=fingerprint,
                message=message,
                channel=channel,
                payload=payload,
                cooldown_seconds=cooldown_seconds,
            )
            conn.commit()
            return alert_id
    except Exception:
        return None


def list_alerts(
    conn: psycopg.Connection,
    *,
    limit: int = 50,
    severity: str | None = None,
    project: str | None = None,
) -> list[Alert]:
    clauses: list[str] = []
    params: list[Any] = []
    if severity:
        clauses.append("severity = %s")
        params.append(_severity(severity))
    if project:
        clauses.append("project = %s")
        params.append(project)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, ts, severity, project, fingerprint, message, channel, payload
            FROM alerts
            {where}
            ORDER BY ts DESC
            LIMIT %s
            """,
            params,
        )
        return [
            Alert(
                id=str(row[0]),
                ts=row[1],
                severity=row[2],
                project=row[3],
                fingerprint=row[4],
                message=row[5],
                channel=row[6],
                payload=row[7] or {},
            )
            for row in cur.fetchall()
        ]


def _severity(value: str) -> str:
    severity = str(value)
    if severity not in SEVERITIES:
        raise ValueError(f"invalid alert severity: {severity}")
    return severity


def _channel(value: str) -> str:
    channel = str(value)
    if channel not in CHANNELS:
        raise ValueError(f"invalid alert channel: {channel}")
    return channel
