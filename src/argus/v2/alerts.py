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
    alert_payload = payload or {}
    if _same_lineage_alert(conn, project, resolved_channel, alert_payload):
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
                Json(alert_payload),
            ),
        )
        return str(cur.fetchone()[0])


def _same_lineage_alert(
    conn: psycopg.Connection,
    project: str,
    channel: str,
    payload: dict[str, Any],
) -> bool:
    if not payload.get("collapse_lineage"):
        return False
    lineage = _payload_text(
        payload, "lineage", "request_lineage", "original_request_lineage"
    )
    if not lineage:
        return False
    readiness = _payload_text(payload, "readiness_signal", "readiness_state")
    blocker = _payload_text(payload, "blocker", "blocker_key", "blocker_state")
    escalation = _payload_text(
        payload, "escalation", "escalation_level", "escalation_threshold"
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT payload
            FROM alerts
            WHERE project=%s
              AND channel=%s
              AND payload->>'collapse_lineage' = 'true'
              AND COALESCE(payload->>'lineage',
                           payload->>'request_lineage',
                           payload->>'original_request_lineage') = %s
            ORDER BY ts DESC
            LIMIT 1
            """,
            (str(project), channel, lineage),
        )
        row = cur.fetchone()
    if row is None:
        return False
    previous = row[0] or {}
    return (
        readiness == _payload_text(previous, "readiness_signal", "readiness_state")
        and blocker == _payload_text(previous, "blocker", "blocker_key", "blocker_state")
        and escalation == _payload_text(
            previous, "escalation", "escalation_level", "escalation_threshold"
        )
    )


def _payload_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return str(value)
    return ""


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
