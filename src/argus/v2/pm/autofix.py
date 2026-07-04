"""v2 PM auto-fix dispatch over the durable core."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

import psycopg

from argus.v2.ingress import events
from argus.v2.orchestrator import pipeline

_ACTIONABLE = frozenset({"warn", "error", "critical"})


@dataclass(frozen=True)
class DispatchResult:
    fingerprint: str
    request_id: str | None
    event_id: str | None
    status: str


def dispatch(conn: psycopg.Connection, cfg, *, project: str, fingerprint: str,
             finding: dict | None = None, message: str | None = None,
             source: str = "pm", force: bool = False) -> DispatchResult:
    team = cfg.team(project)
    if team.project is None:
        raise ValueError(f"team {project} has no project config")
    row = finding or load_finding(conn, project, fingerprint)
    if row is None:
        if not message:
            raise FileNotFoundError(f"unknown finding {fingerprint} for {project}")
        row = {"fingerprint": fingerprint, "message": message, "severity": "warn"}
    payload = dict(row)
    payload.setdefault("fingerprint", fingerprint)
    payload["text"] = _finding_text(payload)
    coalesced_request_id = _coalesced_dispatch_request(
        conn, project=project, fingerprint=fingerprint, payload=payload,
    )
    if coalesced_request_id:
        _record_dispatch(
            conn, project, fingerprint, f"{source}:coalesced", coalesced_request_id,
        )
        return DispatchResult(fingerprint, coalesced_request_id, None, "coalesced")
    if not force:
        _check_daily_limit(conn, project, int(team.project.pm.daily_limit))
    event_id = events.ingest_signal(
        conn, cfg, team=project, source=f"pm:{source}",
        fingerprint=fingerprint, payload=payload,
    )
    request_id = pipeline.open_request(
        conn, cfg, event_id=event_id, team_id=project,
        conversation_id=None, fingerprint=fingerprint,
    )
    if request_id is None:
        return DispatchResult(fingerprint, None, event_id, "active")
    _record_dispatch(conn, project, fingerprint, source, request_id)
    return DispatchResult(fingerprint, request_id, event_id, "queued")


def cycle(conn: psycopg.Connection, cfg, *, project: str) -> list[DispatchResult]:
    results: list[DispatchResult] = []
    seen: set[str] = set()
    for row in actionable_findings(conn, project):
        fingerprint = str(row.get("fingerprint") or "")
        if not fingerprint or fingerprint in seen:
            continue
        seen.add(fingerprint)
        severity = str(row.get("severity") or "info").lower()
        if severity not in _ACTIONABLE:
            continue
        try:
            result = dispatch(conn, cfg, project=project, fingerprint=fingerprint,
                              finding=row, source="pm-cycle")
        except RuntimeError as exc:
            if "daily cap" in str(exc):
                break
            raise
        results.append(result)
    return results


def load_finding(conn: psycopg.Connection, project: str, fingerprint: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT severity, message, payload
            FROM alerts
            WHERE project=%s AND fingerprint=%s
            ORDER BY ts DESC
            LIMIT 1
            """,
            (project, fingerprint),
        )
        row = cur.fetchone()
        if row:
            severity, message, payload = row
            data = dict(payload or {})
            data.setdefault("fingerprint", fingerprint)
            data.setdefault("severity", severity)
            data.setdefault("message", message)
            return data
        cur.execute(
            """
            SELECT payload
            FROM events
            WHERE team_id=%s AND kind='signal' AND dedup_key=%s
            ORDER BY received_at DESC
            LIMIT 1
            """,
            (project, fingerprint),
        )
        row = cur.fetchone()
    if not row:
        return None
    data = dict(row[0] or {})
    data.setdefault("fingerprint", fingerprint)
    return data


def actionable_findings(conn: psycopg.Connection, project: str, limit: int = 50) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.fingerprint, a.severity, a.message, a.payload
            FROM alerts a
            WHERE a.project=%s
              AND a.severity = ANY(%s)
              AND NOT EXISTS (
                SELECT 1 FROM pm_dispatches d
                WHERE d.team_id=a.project AND d.fingerprint=a.fingerprint
              )
            ORDER BY a.ts ASC
            LIMIT %s
            """,
            (project, list(_ACTIONABLE), int(limit)),
        )
        rows = cur.fetchall()
    out: list[dict] = []
    for fingerprint, severity, message, payload in rows:
        data = dict(payload or {})
        data.setdefault("fingerprint", fingerprint)
        data.setdefault("severity", severity)
        data.setdefault("message", message)
        out.append(data)
    return out


def _finding_text(row: dict) -> str:
    message = str(row.get("message") or row.get("title") or "").strip()
    if message:
        return message
    return json.dumps(row, sort_keys=True, separators=(",", ":"))


def _coalesced_dispatch_request(conn: psycopg.Connection, *, project: str,
                                fingerprint: str, payload: dict) -> str | None:
    theme = _normalize_dispatch_key_part(payload.get("theme"))
    task = _normalize_dispatch_key_part(payload.get("text"))
    if not theme or not task:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.request_id::text, e.payload
            FROM pm_dispatches d
            JOIN requests r ON r.id=d.request_id
            JOIN events e ON e.id=r.event_id
            WHERE d.team_id=%s
              AND d.fingerprint<>%s
              AND d.request_id IS NOT NULL
            ORDER BY d.created_at DESC
            """,
            (project, fingerprint),
        )
        rows = cur.fetchall()
    for request_id, existing_payload in rows:
        existing_payload = existing_payload or {}
        if _normalize_dispatch_key_part(existing_payload.get("theme")) != theme:
            continue
        if _normalize_dispatch_key_part(existing_payload.get("text")) != task:
            continue
        return str(request_id)
    return None


def _normalize_dispatch_key_part(value) -> str:
    return " ".join(str(value or "").lower().split())


def _check_daily_limit(conn: psycopg.Connection, project: str, limit: int) -> None:
    if limit <= 0:
        raise RuntimeError(f"pm breaker: {project} daily cap disabled")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pm_dispatches "
            "WHERE team_id=%s AND dispatch_day=current_date "
            "AND source NOT LIKE %s",
            (project, "%:coalesced"),
        )
        count = int(cur.fetchone()[0])
    if count >= limit:
        raise RuntimeError(f"pm breaker: {project} reached daily cap ({count}/{limit})")


def _record_dispatch(conn: psycopg.Connection, project: str, fingerprint: str,
                     source: str, request_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pm_dispatches (team_id, fingerprint, source, request_id) "
            "VALUES (%s,%s,%s,%s)",
            (project, fingerprint, source, request_id),
        )


def safe_fingerprint(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", value)
