"""Completion watchdog for accepted requests.

It alerts on accepted work that later fails or stops progressing. It never
retries automatically; retry is a human CLI action.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.types.json import Json

from argus.v2.actions import executor

CONTENT_SOURCES = {
    "content-approval-watch",
    "pm:content-approval-watch",
    "content-team-run",
    "pm:content-team-run",
}
CONTENT_PREFIXES = ("content-approval:", "content-team-run:")
LIVE_CONTENT_ACTIONS = {"publish", "schedule", "media", "cta"}


@dataclass(frozen=True)
class WatchdogFinding:
    request_id: str
    team_id: str
    category: str
    reason: str
    source: str
    fingerprint: str
    request_status: str
    current_stage: int
    job_id: str | None
    job_role: str | None
    job_stage: int | None
    job_status: str | None
    failure_reason: str
    retry_command: str
    retryable: bool
    destination_ref: str


@dataclass(frozen=True)
class RetryResult:
    ok: bool
    request_id: str
    job_id: str | None
    status: str
    reason: str = ""


@dataclass(frozen=True)
class _Rule:
    threshold_minutes: int
    destination_ref: str | None = None
    sources: tuple[str, ...] = ()
    fingerprint_prefixes: tuple[str, ...] = ()


def run(conn: psycopg.Connection, cfg, *, threshold_minutes: int | None = None,
        drain: bool = False) -> int:
    findings = find(conn, cfg, threshold_minutes=threshold_minutes)
    inserted = 0
    for finding in findings:
        inserted += _insert_alert(conn, finding)
    if drain and inserted:
        executor.process_proposed(conn, cfg)
    return inserted


def find(conn: psycopg.Connection, cfg, *, threshold_minutes: int | None = None,
         now: datetime | None = None) -> list[WatchdogFinding]:
    now = now or datetime.now(timezone.utc)
    rows = _candidate_rows(conn)
    findings: list[WatchdogFinding] = []
    for row in rows:
        row = dict(row)
        rule = _matching_rule(cfg, row)
        if rule is None:
            continue
        threshold = int(threshold_minutes or rule.threshold_minutes or 45)
        finding = _classify(row, cfg, rule=rule, threshold_minutes=threshold, now=now)
        if finding is not None:
            findings.append(finding)
    return findings


def retry_request(conn: psycopg.Connection, cfg, request_id: str, *,
                  force_live: bool = False, force_stuck: bool = False) -> RetryResult:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT r.id::text AS request_id, r.team_id, r.status AS request_status,
                   r.current_stage, r.fingerprint, e.source, e.payload,
                   j.id::text AS job_id, j.status AS job_status,
                   j.lease_expires_at
            FROM requests r
            JOIN events e ON e.id = r.event_id
            LEFT JOIN LATERAL (
                SELECT id, status, lease_expires_at
                FROM jobs
                WHERE request_id = r.id AND kind='pipeline'
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 1
            ) j ON true
            WHERE r.id=%s
            """,
            (request_id,),
        )
        row = cur.fetchone()
    if not row:
        return RetryResult(False, request_id, None, "missing", "request not found")
    mode = _content_mode(str(row.get("source") or ""), str(row.get("fingerprint") or ""),
                         row.get("payload") or {})
    if mode == "live" and not force_live:
        return RetryResult(False, request_id, row.get("job_id"), "needs-force-live",
                           "live content action requires --force-live after manual gate check")
    job_id = row.get("job_id")
    if not job_id:
        return RetryResult(False, request_id, None, "no-job", "request has no pipeline job")
    job_status = str(row.get("job_status") or "")
    if job_status in {"claimed", "running"}:
        lease = row.get("lease_expires_at")
        lease_alive = lease is not None and lease > datetime.now(timezone.utc)
        if lease_alive and not force_stuck:
            return RetryResult(False, request_id, job_id, "leased",
                               "job still has active lease; use --force-stuck only after checking worker logs")
    if job_status not in {"pending", "claimed", "running", "failed", "dead"}:
        return RetryResult(False, request_id, job_id, "not-retryable",
                           f"job status is {job_status}")
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE requests
            SET status='open', updated_at=now()
            WHERE id=%s AND status IN ('open','failed','awaiting_approval')
            """,
            (request_id,),
        )
        cur.execute(
            """
            UPDATE jobs
            SET status='pending',
                attempts=0,
                max_attempts=GREATEST(max_attempts, attempts + 1),
                claim_token=NULL,
                claimed_by=NULL,
                claimed_at=NULL,
                lease_expires_at=NULL,
                heartbeat_at=NULL,
                run_after=now(),
                advanced_at=NULL,
                updated_at=now()
            WHERE id=%s
            """,
            (job_id,),
        )
    return RetryResult(True, request_id, job_id, "queued")


def _candidate_rows(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT r.id::text AS request_id, r.team_id, r.status AS request_status,
                   r.current_stage, r.fingerprint, r.created_at AS request_created_at,
                   r.updated_at AS request_updated_at,
                   e.source, e.payload,
                   j.id::text AS job_id, j.role AS job_role, j.stage AS job_stage,
                   j.kind AS job_kind, j.status AS job_status, j.result AS job_result,
                   j.created_at AS job_created_at, j.updated_at AS job_updated_at,
                   j.heartbeat_at AS job_heartbeat_at,
                   COALESCE(job_counts.n, 0) AS job_count,
                   latest_run.output AS latest_run_output
            FROM requests r
            JOIN events e ON e.id = r.event_id
            LEFT JOIN LATERAL (
                SELECT id, role, stage, kind, status, result, created_at, updated_at,
                       heartbeat_at
                FROM jobs
                WHERE request_id = r.id AND kind='pipeline'
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 1
            ) j ON true
            LEFT JOIN LATERAL (
                SELECT count(*) AS n
                FROM jobs
                WHERE request_id = r.id AND kind='pipeline'
            ) job_counts ON true
            LEFT JOIN LATERAL (
                SELECT output
                FROM runs
                WHERE job_id = j.id
                ORDER BY started_at DESC
                LIMIT 1
            ) latest_run ON true
            WHERE r.status IN ('open','awaiting_approval','failed')
              AND r.created_at > now() - interval '14 days'
            ORDER BY r.created_at ASC
            LIMIT 500
            """
        )
        return list(cur.fetchall())


def _matching_rule(cfg, row: dict[str, Any]) -> _Rule | None:
    source = str(row.get("source") or "")
    fingerprint = str(row.get("fingerprint") or "")
    team_id = str(row.get("team_id") or "")
    content_match = (
        team_id == "content"
        and (source in CONTENT_SOURCES or fingerprint.startswith(CONTENT_PREFIXES))
    )
    if content_match:
        return _Rule(
            threshold_minutes=_env_int(
                "ARGUS_CONTENT_WATCHDOG_THRESHOLD_MINUTES",
                _env_int("ARGUS_COMPLETION_WATCHDOG_THRESHOLD_MINUTES", 45),
            ),
            destination_ref=os.environ.get("ARGUS_CONTENT_WATCHDOG_DESTINATION") or None,
            sources=tuple(CONTENT_SOURCES),
            fingerprint_prefixes=CONTENT_PREFIXES,
        )
    for wd in _configured_watchdogs(cfg, team_id):
        if not getattr(wd, "enabled", False):
            continue
        sources = tuple(str(s) for s in getattr(wd, "sources", []) or ())
        prefixes = tuple(str(p) for p in getattr(wd, "fingerprint_prefixes", []) or ())
        if sources and source not in sources:
            continue
        if prefixes and not fingerprint.startswith(prefixes):
            continue
        return _Rule(
            threshold_minutes=int(getattr(wd, "threshold_minutes", 45) or 45),
            destination_ref=getattr(wd, "destination_ref", None),
            sources=sources,
            fingerprint_prefixes=prefixes,
        )
    return None


def _configured_watchdogs(cfg, team_id: str):
    try:
        team = cfg.team(team_id)
    except KeyError:
        team = None
    if team is not None and getattr(team, "completion_watchdog", None) is not None:
        yield team.completion_watchdog
    default = getattr(getattr(cfg, "company", None), "defaults", None)
    if default is not None:
        yield getattr(default, "completion_watchdog", None)


def _classify(row: dict[str, Any], cfg, *, rule: _Rule, threshold_minutes: int,
              now: datetime) -> WatchdogFinding | None:
    request_status = str(row.get("request_status") or "")
    job_status = str(row.get("job_status") or "")
    category = ""
    reason = ""
    if request_status == "failed" or job_status in {"failed", "dead"}:
        category = "failed"
        reason = "request or pipeline job failed"
    elif int(row.get("job_count") or 0) == 0:
        if _minutes_old(row.get("request_created_at"), now) < threshold_minutes:
            return None
        category = "stuck"
        reason = "request has no pipeline job"
    else:
        progress_at = (
            row.get("job_heartbeat_at")
            or row.get("job_updated_at")
            or row.get("job_created_at")
            or row.get("request_updated_at")
        )
        if job_status in {"pending", "claimed", "running"} and (
            _minutes_old(progress_at, now) >= threshold_minutes
        ):
            category = "stuck"
            reason = f"job has no progress for >= {threshold_minutes} minutes"
        elif request_status in {"open", "awaiting_approval"} and (
            _minutes_old(row.get("request_created_at"), now) >= threshold_minutes
        ):
            category = "slow"
            reason = f"request open for >= {threshold_minutes} minutes"
    if not category:
        return None
    request_id = str(row["request_id"])
    source = str(row.get("source") or "")
    fingerprint = str(row.get("fingerprint") or "")
    payload = row.get("payload") or {}
    mode = _content_mode(source, fingerprint, payload)
    retryable = mode != "live"
    retry_command = f"argus request retry {request_id}"
    if mode == "live":
        retry_command += " --force-live"
    if job_status in {"claimed", "running"}:
        retry_command += " --force-stuck"
    dest = rule.destination_ref or _team_control_dest(cfg, str(row["team_id"])) or "cli:local"
    return WatchdogFinding(
        request_id=request_id,
        team_id=str(row["team_id"]),
        category=category,
        reason=reason,
        source=source,
        fingerprint=fingerprint,
        request_status=request_status,
        current_stage=int(row.get("current_stage") or 0),
        job_id=row.get("job_id"),
        job_role=row.get("job_role"),
        job_stage=row.get("job_stage"),
        job_status=job_status or None,
        failure_reason=_failure_reason(row) if category == "failed" else "",
        retry_command=retry_command,
        retryable=retryable,
        destination_ref=dest,
    )


def _insert_alert(conn: psycopg.Connection, finding: WatchdogFinding) -> int:
    text = _alert_text(finding)
    job_id = finding.job_id
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO actions (request_id, job_id, team_id, type, risk,
                                 destination_ref, idempotency_key, payload)
            VALUES (%s,%s,%s,'notify','reversible_internal',%s,%s,%s)
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            (
                finding.request_id,
                job_id,
                finding.team_id,
                finding.destination_ref,
                _alert_idempotency_key(finding),
                Json({"text": text, "urgent": True, "severity": "error"}),
            ),
        )
        return 1 if cur.rowcount else 0


def _alert_idempotency_key(finding: WatchdogFinding) -> str:
    lineage = _content_approval_lineage(finding.fingerprint)
    if not lineage:
        return f"completion-watchdog:{finding.category}:{finding.request_id}"
    state = "|".join(
        [
            finding.category,
            finding.request_status,
            str(finding.current_stage),
            finding.job_role or "none",
            str(finding.job_stage) if finding.job_stage is not None else "none",
            finding.job_status or "none",
            finding.reason,
            finding.failure_reason,
        ]
    )
    digest = hashlib.sha256(state.encode("utf-8")).hexdigest()[:16]
    return f"completion-watchdog:{lineage}:{digest}"


def _content_approval_lineage(fingerprint: str) -> str:
    parts = str(fingerprint or "").split(":")
    if len(parts) >= 5 and parts[0] == "content-approval":
        return ":".join(parts[:4])
    return ""


def _alert_text(finding: WatchdogFinding) -> str:
    stage = (
        f"request_stage={finding.current_stage}, "
        f"job={finding.job_id or 'none'}, role={finding.job_role or 'none'}, "
        f"job_stage={finding.job_stage if finding.job_stage is not None else 'none'}, "
        f"job_status={finding.job_status or 'none'}"
    )
    lines = [
        "Argus completion watchdog alert",
        f"Request: {finding.request_id}",
        f"Team: {finding.team_id}",
        f"Fingerprint: {finding.fingerprint or 'none'}",
        f"Source: {finding.source or 'unknown'}",
        f"Status: {finding.request_status}",
        f"Current stage/job: {stage}",
        f"Reason: {finding.reason}",
    ]
    if finding.failure_reason:
        lines.append(f"Failure: {finding.failure_reason}")
    if finding.retryable:
        lines.append(f"Suggested retry: `{finding.retry_command}`")
    else:
        lines.append(f"Manual live retry only: `{finding.retry_command}`")
    if finding.source in CONTENT_SOURCES or finding.fingerprint.startswith(CONTENT_PREFIXES):
        lines.append(
            "Safety: watchdog only alerts. It does not publish, schedule, upload media, or change CTA routes."
        )
    return "\n".join(lines)


def _failure_reason(row: dict[str, Any]) -> str:
    result = row.get("job_result")
    if isinstance(result, dict):
        for key in ("test_output", "output", "error"):
            value = str(result.get(key) or "").strip()
            if value:
                return value[-1000:]
        parsed = result.get("parsed")
        if isinstance(parsed, dict):
            value = str(parsed.get("analysis") or "").strip()
            if value:
                return value[-1000:]
    value = str(row.get("latest_run_output") or "").strip()
    return value[-1000:] if value else "unknown"


def _content_mode(source: str, fingerprint: str, payload: dict[str, Any]) -> str:
    if source not in CONTENT_SOURCES and not fingerprint.startswith(CONTENT_PREFIXES):
        return "generic"
    text = str((payload or {}).get("text") or "")
    lower = text.lower()
    if fingerprint.startswith("content-team-run:"):
        return "draft"
    if "approved action: draft" in lower or "internal draft" in lower:
        return "draft"
    match = re.search(r"approved action:\s*([a-z]+)", lower)
    if match and match.group(1) in LIVE_CONTENT_ACTIONS:
        return "live"
    if any(f"approved action: {action}" in lower for action in LIVE_CONTENT_ACTIONS):
        return "live"
    return "generic"


def _team_control_dest(cfg, team_id: str) -> str | None:
    try:
        team = cfg.team(team_id)
    except KeyError:
        return None
    for channel in getattr(team, "channels", []) or []:
        if channel.role == "control" and channel.type != "cli":
            return f"{channel.type}:{channel.channel_id}"
    return None


def _minutes_old(value, now: datetime) -> float:
    if value is None:
        return 0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (now - value).total_seconds() / 60.0


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default
