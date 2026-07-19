"""Evidence-backed proactive maintenance intake.

Maintenance is deliberately not a brainstorming loop. It only dispatches work
that can be tied to a current durable Argus record from a configured source.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

import psycopg

from argus.v2.ownership import store
from argus.v2.pm import autofix


_ACTIONABLE = frozenset({"warn", "error", "critical", "medium", "high"})
_RESOLVED = frozenset({"closed", "resolved", "done", "fixed", "cancelled"})
_SEVERITY_PRIORITY = {
    "critical": 400,
    "error": 300,
    "high": 300,
    "warn": 200,
    "medium": 200,
    "info": 100,
    "low": 100,
}
_GITHUB_ISSUE = re.compile(
    r"^https://github\.com/([^/]+/[^/]+)/issues/([1-9][0-9]*)/?$",
    re.IGNORECASE,
)
_GITHUB_PR = re.compile(
    r"^https://github\.com/([^/]+/[^/]+)/pull/([1-9][0-9]*)/?$",
    re.IGNORECASE,
)
_DRAFT_FAILURE_MARKERS = (
    "fail",
    "blocking",
    "needs review",
    "found root cause",
    "not fixed",
)


@dataclass(frozen=True)
class Candidate:
    fingerprint: str
    severity: str
    title: str
    request_message: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)
    source_ref: str | None = None
    observed_at: datetime | None = None

    @property
    def priority(self) -> int:
        return _SEVERITY_PRIORITY.get(self.severity.lower(), 0)


class _Reschedule(Exception):
    pass


def collect_candidates(conn: psycopg.Connection, cfg, team_id: str) -> list[Candidate]:
    """Return current, concrete maintenance evidence for one configured team."""
    try:
        team = cfg.team(team_id)
    except KeyError:
        return []
    if not team.ownership.enabled or not team.ownership.maintenance.enabled:
        return []
    if team.project is None:
        return []

    sources = _configured_sources(cfg, team_id)
    if not sources:
        return _sort_candidates([
            *_failed_request_candidates(conn, team_id),
            *_draft_failure_candidates(conn, team),
        ])

    candidates = [
        *_connector_event_candidates(conn, team_id, sources),
        *_connector_alert_candidates(conn, team_id, sources),
        *_failed_request_candidates(conn, team_id),
        *_draft_failure_candidates(conn, team),
    ]
    unique: dict[str, Candidate] = {}
    for candidate in candidates:
        unique.setdefault(candidate.fingerprint, candidate)
    return _sort_candidates(list(unique.values()))


def dispatch_one(
    conn: psycopg.Connection,
    cfg,
    team_id: str,
    candidates: list[Candidate],
) -> str | None:
    """Dispatch the highest-priority still-eligible candidate once."""
    if conn.autocommit:
        raise ValueError("maintenance dispatch requires autocommit=False")
    if not candidates:
        return None
    try:
        team = cfg.team(team_id)
    except KeyError:
        return None
    policy = team.ownership.maintenance
    if (
        not team.ownership.enabled
        or not policy.enabled
        or team.project is None
        or not _try_team_lock(conn, team_id)
    ):
        return None
    if _interval_blocked(conn, team_id, policy.interval_hours):
        return None
    if _open_count(conn, team_id) >= policy.max_open:
        return None
    if _active_count(conn, team_id) >= team.ownership.max_active_obligations:
        return None

    requested = {item.fingerprint for item in candidates}
    current = [
        item
        for item in collect_candidates(conn, cfg, team_id)
        if item.fingerprint in requested
        and _complete_candidate(item, team_id)
        and not _already_owned(conn, team_id, item)
    ]
    if not current:
        return None
    candidate = _sort_candidates(current)[0]
    obligation_fingerprint = f"event:{candidate.fingerprint}"

    try:
        with conn.transaction():
            obligation = store.upsert(
                conn,
                team_id=team_id,
                kind="maintenance",
                fingerprint=obligation_fingerprint,
                title=candidate.title,
                source_ref=candidate.source_ref,
                definition_of_done={
                    "healthy": True,
                    "evidence_fingerprint": candidate.fingerprint,
                },
            )
            if obligation.kind != "maintenance" or obligation.request_id is not None:
                raise _Reschedule
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE team_obligations SET priority=%s WHERE id=%s",
                    (candidate.priority, obligation.id),
                )
            store.transition(
                conn,
                obligation.id,
                to_status="open",
                reason="maintenance candidate selected",
                evidence={"candidate": dict(candidate.evidence)},
            )
            result = autofix.dispatch(
                conn,
                cfg,
                project=team_id,
                fingerprint=candidate.fingerprint,
                finding={
                    "fingerprint": candidate.fingerprint,
                    "severity": candidate.severity,
                    "title": candidate.title,
                    "message": candidate.request_message,
                    "source_ref": candidate.source_ref,
                    "evidence": dict(candidate.evidence),
                },
                source="ownership-maintenance",
            )
            if result.request_id is None:
                raise _Reschedule
            linked = store.get(conn, obligation.id)
            if linked is None or str(linked.request_id) != result.request_id:
                raise RuntimeError("maintenance request was not linked to its obligation")
            return result.request_id
    except _Reschedule:
        return None
    except RuntimeError as exc:
        message = str(exc).lower()
        if "daily cap" in message or "temporarily unavailable" in message:
            return None
        raise


def _configured_sources(cfg, team_id: str) -> dict[str, Any]:
    team = cfg.team(team_id)
    out = {source.name: source for source in team.sources}
    for source in cfg.company.sources:
        if source.team == team_id:
            out.setdefault(source.name, source)
    return out


def _source_by_marker(sources: dict[str, Any], marker: str) -> Any | None:
    if marker in sources:
        return sources[marker]
    typed = [source for source in sources.values() if source.type == marker]
    return typed[0] if len(typed) == 1 else None


def _connector_event_candidates(
    conn: psycopg.Connection,
    team_id: str,
    sources: dict[str, Any],
) -> list[Candidate]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.id::text, e.source, e.dedup_key, e.payload, e.received_at
            FROM events e
            WHERE e.team_id=%s
              AND e.kind='signal'
              AND e.source = ANY(%s)
              AND e.received_at >= clock_timestamp() - interval '30 days'
              AND e.source NOT LIKE 'pm:%%'
              AND NOT EXISTS (SELECT 1 FROM requests r WHERE r.event_id=e.id)
            ORDER BY e.received_at, e.id
            """,
            (team_id, list(sources)),
        )
        rows = cur.fetchall()
    out: list[Candidate] = []
    for event_id, source_name, raw_fingerprint, raw_payload, observed_at in rows:
        source = sources.get(source_name)
        payload = dict(raw_payload or {})
        if source is None or _is_resolved(payload):
            continue
        if source.type == "github":
            candidate = _github_candidate(
                team_id,
                source,
                event_id,
                str(raw_fingerprint),
                payload,
                observed_at,
            )
        else:
            candidate = _connector_candidate(
                team_id,
                source,
                event_id,
                str(raw_fingerprint),
                payload,
                observed_at,
            )
        if candidate is not None:
            out.append(candidate)
    return out


def _connector_alert_candidates(
    conn: psycopg.Connection,
    team_id: str,
    sources: dict[str, Any],
) -> list[Candidate]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (fingerprint)
                   id::text, fingerprint, severity, message, payload, ts
            FROM alerts
            WHERE project=%s
              AND ts >= clock_timestamp() - interval '30 days'
            ORDER BY fingerprint, ts DESC, id DESC
            """,
            (team_id,),
        )
        rows = cur.fetchall()
    out: list[Candidate] = []
    for alert_id, raw_fingerprint, severity, message, raw_payload, observed_at in rows:
        payload = dict(raw_payload or {})
        marker = str(payload.get("source") or "").strip()
        source = _source_by_marker(sources, marker) if marker else None
        if source is None or _is_resolved(payload):
            continue
        normalized = _severity(severity)
        title = _clean_text(message)
        if normalized not in _ACTIONABLE or not title:
            continue
        fingerprint = f"alert:{source.name}:{raw_fingerprint}"
        evidence = {
            "source_kind": "connector_alert",
            "source_id": alert_id,
            "source_name": source.name,
            "source_type": source.type,
            "team_id": team_id,
            "fingerprint": str(raw_fingerprint),
            "severity": normalized,
            "message": title,
            "observed_at": _iso(observed_at),
        }
        out.append(_candidate(
            fingerprint,
            normalized,
            title,
            evidence,
            f"alert:{alert_id}",
            observed_at,
            f"connector alert {source.name}",
        ))
    return out


def _connector_candidate(
    team_id: str,
    source,
    event_id: str,
    raw_fingerprint: str,
    payload: dict[str, Any],
    observed_at: datetime,
) -> Candidate | None:
    marker = str(payload.get("source") or "").strip()
    if marker not in {source.name, source.type}:
        return None
    severity = _severity(payload.get("severity"))
    title = _clean_text(payload.get("message") or payload.get("title"))
    if severity not in _ACTIONABLE or not title or not raw_fingerprint:
        return None
    fingerprint = f"connector:{source.name}:{raw_fingerprint}"
    evidence = {
        "source_kind": "connector_event",
        "source_id": event_id,
        "source_name": source.name,
        "source_type": source.type,
        "team_id": team_id,
        "fingerprint": raw_fingerprint,
        "severity": severity,
        "message": title,
        "observed_at": _iso(observed_at),
    }
    return _candidate(
        fingerprint,
        severity,
        title,
        evidence,
        f"event:{event_id}",
        observed_at,
        f"connector event {source.name}",
    )


def _github_candidate(
    team_id: str,
    source,
    event_id: str,
    raw_fingerprint: str,
    payload: dict[str, Any],
    observed_at: datetime,
) -> Candidate | None:
    labels = source.config.get("labels")
    if isinstance(labels, str):
        labels = [labels]
    repo = str(source.config.get("repo") or "").strip().lower()
    project = str(source.config.get("project") or team_id).strip()
    if (
        not labels
        or not repo
        or project != team_id
        or payload.get("kind") != "issue"
        or payload.get("source") != "github"
    ):
        return None
    try:
        number = int(payload.get("number"))
    except (TypeError, ValueError):
        return None
    match = _GITHUB_ISSUE.fullmatch(str(payload.get("url") or "").strip())
    if not match or match.group(1).lower() != repo or int(match.group(2)) != number:
        return None
    title = _clean_text(payload.get("title"))
    if not title:
        return None
    severity = _severity(payload.get("severity") or "warn")
    if severity not in _ACTIONABLE:
        return None
    message = _clean_text(payload.get("message") or title)
    fingerprint = f"github:{source.name}:{number}"
    evidence = {
        "source_kind": "github_issue",
        "source_id": event_id,
        "source_name": source.name,
        "source_type": "github",
        "team_id": team_id,
        "fingerprint": raw_fingerprint,
        "issue_number": number,
        "repo": repo,
        "url": str(payload["url"]),
        "severity": severity,
        "message": message,
        "observed_at": _iso(observed_at),
    }
    return _candidate(
        fingerprint,
        severity,
        title,
        evidence,
        f"event:{event_id}",
        observed_at,
        f"configured GitHub issue {source.name}",
    )


def _failed_request_candidates(
    conn: psycopg.Connection,
    team_id: str,
) -> list[Candidate]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id::text, r.fingerprint, r.updated_at,
                   e.id::text, e.source, e.payload
            FROM requests r
            JOIN events e ON e.id=r.event_id
            WHERE r.team_id=%s
              AND r.status='failed'
              AND r.updated_at >= clock_timestamp() - interval '30 days'
              AND e.source NOT LIKE 'pm:ownership-maintenance%%'
              AND NOT EXISTS (
                SELECT 1 FROM team_obligations o WHERE o.request_id=r.id
              )
            ORDER BY r.updated_at, r.id
            """,
            (team_id,),
        )
        rows = cur.fetchall()
    out: list[Candidate] = []
    for request_id, request_fingerprint, observed_at, event_id, source, raw_payload in rows:
        payload = dict(raw_payload or {})
        title = _clean_text(
            payload.get("text") or payload.get("message") or payload.get("title")
        )
        if not title:
            continue
        fingerprint = f"request:{request_id}"
        evidence = {
            "source_kind": "failed_request",
            "source_id": request_id,
            "team_id": team_id,
            "request_id": request_id,
            "request_fingerprint": str(request_fingerprint or ""),
            "event_id": event_id,
            "event_source": str(source),
            "status": "failed",
            "message": title,
            "observed_at": _iso(observed_at),
        }
        out.append(_candidate(
            fingerprint,
            "high",
            f"Repair failed request: {title}",
            evidence,
            f"request:{request_id}",
            observed_at,
            f"failed Argus request {request_id}",
        ))
    return out


def _draft_failure_candidates(
    conn: psycopg.Connection,
    team,
) -> list[Candidate]:
    team_id = team.name
    configured_repo = str(team.project.github_repo or "").strip().lower()
    if not configured_repo:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.id::text, a.request_id::text, a.provider_ref, a.payload,
                   a.updated_at
            FROM actions a
            WHERE a.team_id=%s
              AND a.type='open_pr'
              AND a.status='done'
              AND a.payload->>'draft'='true'
              AND a.updated_at >= clock_timestamp() - interval '30 days'
              AND NOT EXISTS (
                SELECT 1 FROM team_obligations o WHERE o.request_id=a.request_id
              )
            ORDER BY a.updated_at, a.id
            """,
            (team_id,),
        )
        rows = cur.fetchall()
    out: list[Candidate] = []
    for action_id, request_id, provider_ref, raw_payload, observed_at in rows:
        payload = dict(raw_payload or {})
        risk = _clean_text(payload.get("risk_summary"))
        if not risk or not any(marker in risk.lower() for marker in _DRAFT_FAILURE_MARKERS):
            continue
        match = _GITHUB_PR.fullmatch(str(provider_ref or "").strip())
        title = _clean_text(payload.get("title"))
        if (
            match is None
            or not title
            or not request_id
            or (configured_repo and match.group(1).lower() != configured_repo)
        ):
            continue
        severity = "critical" if "critical" in risk.lower() else "high"
        fingerprint = f"draft-pr:{action_id}"
        evidence = {
            "source_kind": "draft_pr_failure",
            "source_id": action_id,
            "team_id": team_id,
            "action_id": action_id,
            "request_id": request_id,
            "provider_ref": str(provider_ref),
            "repo": match.group(1).lower(),
            "pull_number": int(match.group(2)),
            "risk_summary": risk,
            "message": title,
            "observed_at": _iso(observed_at),
        }
        out.append(_candidate(
            fingerprint,
            severity,
            f"Repair draft PR failure: {title}",
            evidence,
            f"action:{action_id}",
            observed_at,
            f"Argus draft PR failure {action_id}",
        ))
    return out


def _candidate(
    fingerprint: str,
    severity: str,
    title: str,
    evidence: dict[str, Any],
    source_ref: str,
    observed_at: datetime,
    source_label: str,
) -> Candidate:
    message = str(evidence.get("message") or title)
    request_message = (
        "Investigate and fix this evidence-backed maintenance issue.\n\n"
        f"Title: {title}\n"
        f"Fingerprint: {fingerprint}\n"
        f"Severity: {severity}\n"
        f"Source: {source_label}\n"
        f"Evidence: {message}\n\n"
        "Stay within this evidence. Do not invent adjacent work."
    )
    return Candidate(
        fingerprint=fingerprint,
        severity=severity,
        title=title[:500],
        request_message=request_message,
        evidence=dict(evidence),
        source_ref=source_ref,
        observed_at=observed_at,
    )


def _complete_candidate(candidate: Candidate, team_id: str) -> bool:
    evidence = dict(candidate.evidence)
    return bool(
        candidate.fingerprint
        and candidate.priority > 0
        and candidate.title.strip()
        and candidate.request_message.strip()
        and candidate.source_ref
        and evidence.get("source_kind")
        and evidence.get("source_id")
        and evidence.get("team_id") == team_id
        and evidence.get("observed_at")
        and evidence.get("message")
    )


def _sort_candidates(candidates: list[Candidate]) -> list[Candidate]:
    far_future = datetime.max.replace(tzinfo=timezone.utc)
    return sorted(
        candidates,
        key=lambda item: (
            -item.priority,
            item.observed_at or far_future,
            item.fingerprint,
        ),
    )


def _is_resolved(payload: dict[str, Any]) -> bool:
    if payload.get("resolved") is True:
        return True
    for key in ("status", "state", "resolution"):
        if str(payload.get(key) or "").strip().lower() in _RESOLVED:
            return True
    return False


def _severity(value: Any) -> str:
    return str(value or "info").strip().lower()


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())[:2000]


def _iso(value: datetime) -> str:
    return value.isoformat()


def _try_team_lock(conn: psycopg.Connection, team_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_try_advisory_xact_lock(hashtext('argus-owner:' || %s))",
            (team_id,),
        )
        row = cur.fetchone()
    return bool(row and row[0] is True)


def _interval_blocked(conn: psycopg.Connection, team_id: str, hours: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
              SELECT 1 FROM team_obligations
              WHERE team_id=%s AND kind='maintenance'
                AND created_at > clock_timestamp() - make_interval(hours => %s)
            )
            """,
            (team_id, int(hours)),
        )
        return bool(cur.fetchone()[0])


def _open_count(conn: psycopg.Connection, team_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM team_obligations "
            "WHERE team_id=%s AND kind='maintenance' "
            "AND status NOT IN ('done','failed')",
            (team_id,),
        )
        return int(cur.fetchone()[0])


def _active_count(conn: psycopg.Connection, team_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM team_obligations "
            "WHERE team_id=%s AND status NOT IN ('done','failed')",
            (team_id,),
        )
        return int(cur.fetchone()[0])


def _already_owned(
    conn: psycopg.Connection,
    team_id: str,
    candidate: Candidate,
) -> bool:
    fingerprint = candidate.fingerprint
    request_id = str(candidate.evidence.get("request_id") or "")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
              SELECT 1 FROM team_obligations
              WHERE team_id=%s
                AND (
                  fingerprint = ANY(%s)
                  OR (%s <> '' AND evidence->'candidate'->>'request_id'=%s)
                )
            )
            """,
            (
                team_id,
                [fingerprint, f"event:{fingerprint}"],
                request_id,
                request_id,
            ),
        )
        return bool(cur.fetchone()[0])
