"""Daily team and company learning cycle."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Json

from argus.v2.ingress import events
from argus.v2.knowledge import store as knowledge_store
from argus.v2.orchestrator import pipeline
from argus.v2.pm import memory as pm_memory

COMPANY_TEAM_ID = "__company__"
_AUTO_TYPES = frozenset({"skill", "prompt-edit", "process-edit"})
_TYPES = frozenset({"lesson", "skill", "prompt-edit", "process-edit", "infra-flag"})
_BLOCKERS = (
    ("prompt-injection", ("ignore previous", "ignore all previous", "system prompt")),
    ("secret", ("api_key", "secret", "private key", "AKIA")),
    ("destructive", ("rm -rf", "drop table", "delete all")),
    ("remote-code", ("curl ", "| bash", "wget ")),
)
_PROMPT_DIR = Path(__file__).resolve().parents[3] / "prompts" / "retro"


@dataclass(frozen=True)
class BacklogItem:
    id: str
    team_id: str
    type: str
    status: str
    priority: int
    statement: str
    trigger: str


def scan(text: str) -> list[str]:
    haystack = (text or "").lower()
    return [name for name, needles in _BLOCKERS
            if any(needle.lower() in haystack for needle in needles)]


def record(conn: psycopg.Connection, *, team_id: str, retro_day: date,
           candidates: list[dict], payload: dict[str, Any] | None = None) -> None:
    body = dict(payload or {})
    body["candidates"] = candidates
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO retro_records (team_id, retro_day, payload) VALUES (%s,%s,%s)",
            (team_id, retro_day, Json(body)),
        )


def synthesize(conn: psycopg.Connection, *, retro_day: date | None = None) -> int:
    with conn.cursor() as cur:
        if retro_day is None:
            cur.execute("SELECT team_id, payload FROM retro_records")
        else:
            cur.execute("SELECT team_id, payload FROM retro_records WHERE retro_day=%s",
                        (retro_day,))
        rows = cur.fetchall()
    count = 0
    for team_id, payload in rows:
        for candidate in (payload or {}).get("candidates", []):
            item = _normalize_candidate(team_id, candidate)
            if item is None:
                continue
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO retro_backlog
                      (id, team_id, type, status, priority, statement, trigger, payload)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO UPDATE SET
                      status=EXCLUDED.status,
                      priority=EXCLUDED.priority,
                      statement=EXCLUDED.statement,
                      trigger=EXCLUDED.trigger,
                      payload=retro_backlog.payload || EXCLUDED.payload,
                      updated_at=clock_timestamp()
                    """,
                    (item.id, item.team_id, item.type, item.status, item.priority,
                     item.statement, item.trigger, Json(candidate)),
                )
                count += 1
    return count


def bridge_lessons(conn: psycopg.Connection, cfg=None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, team_id, statement, trigger
            FROM retro_backlog
            WHERE type='lesson' AND status='gated' AND bridged_at IS NULL
            ORDER BY priority DESC, created_at
            """
        )
        rows = cur.fetchall()
    bridged = 0
    for item_id, team_id, statement, trigger in rows:
        if team_id == COMPANY_TEAM_ID:
            if cfg is None:
                continue
            _bridge_company_lesson(conn, cfg, item_id=str(item_id),
                                   statement=statement, trigger=trigger)
        else:
            pm_memory.append(
                conn, team_id=team_id, fingerprint=f"retro:{item_id}",
                finding=statement, outcome="proposed", note=f"retro trigger: {trigger}",
            )
        with conn.cursor() as cur:
            cur.execute("UPDATE retro_backlog SET bridged_at=clock_timestamp() WHERE id=%s",
                        (item_id,))
        bridged += 1
    return bridged


def backlog(conn: psycopg.Connection, *, status: str | None = None,
            team_id: str | None = None) -> list[BacklogItem]:
    sql = ("SELECT id, team_id, type, status, priority, statement, trigger "
           "FROM retro_backlog")
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status=%s")
        params.append(status)
    if team_id:
        clauses.append("team_id=%s")
        params.append(team_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY priority DESC, created_at"
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        return [BacklogItem(*row) for row in cur.fetchall()]


def summary(conn: psycopg.Connection) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT status, count(*) FROM retro_backlog GROUP BY status")
        counts = dict(cur.fetchall())
        cur.execute(
            "SELECT status, count(*) FROM retro_backlog "
            "WHERE team_id=%s GROUP BY status",
            (COMPANY_TEAM_ID,),
        )
        company_counts = dict(cur.fetchall())
        cur.execute(
            "SELECT count(*) FROM retro_backlog "
            "WHERE type=ANY(%s) AND status='gated' AND payload ? 'auto_request_id'",
            (list(_AUTO_TYPES),),
        )
        auto_queued = int(cur.fetchone()[0])
    gated = int(counts.get("gated", 0))
    quarantined = int(counts.get("quarantined", 0))
    infra = int(counts.get("infra-notice", 0))
    company_gated = int(company_counts.get("gated", 0))
    return (f"Retro: {gated} gated, {infra} infra-notice, "
            f"{quarantined} quarantined. Company: {company_gated} gated. "
            f"Auto-changes queued: {auto_queued}.")


def notify(conn: psycopg.Connection, cfg, *, retro_day: date | None = None,
           team_id: str | None = None, company_only: bool = False) -> int:
    day = retro_day or datetime.now(timezone.utc).date()
    count = 0
    if not company_only:
        for team in cfg.teams:
            if team_id and team.name != team_id:
                continue
            if not getattr(team, "project", None):
                continue
            text = _team_digest_text(conn, team.name, day)
            if text:
                count += _queue_notify(
                    conn, cfg, team_id=team.name,
                    destination_ref=_control_destination(cfg, team.name),
                    key=f"retro-digest:{team.name}:{day}",
                    text=text,
                )
    if not team_id:
        text = _company_digest_text(conn, day)
        if text:
            count += _queue_notify(
                conn, cfg, team_id="ceo-brief",
                destination_ref=_control_destination(cfg, "ceo-brief"),
                key=f"retro-digest:{COMPANY_TEAM_ID}:{day}",
                text=text,
            )
    return count


def run(conn: psycopg.Connection, cfg, *, retro_day: date | None = None,
        team_id: str | None = None, company_only: bool = False) -> tuple[int, int]:
    day = retro_day or datetime.now(timezone.utc).date()
    if not company_only:
        for team in cfg.teams:
            if team_id and team.name != team_id:
                continue
            if not getattr(team, "project", None):
                continue
            candidates = _candidates_for_team(conn, cfg, team.name, day)
            if candidates:
                record(conn, team_id=team.name, retro_day=day, candidates=candidates,
                       payload={"role": "team-learning-agent"})
    if not team_id:
        company_candidates = _company_candidates_for_day(conn, cfg, day)
        if company_candidates:
            record(conn, team_id=COMPANY_TEAM_ID, retro_day=day,
                   candidates=company_candidates,
                   payload={"role": "company-learning-agent"})
    synthesized = synthesize(conn, retro_day=day)
    bridged = bridge_lessons(conn, cfg)
    _enqueue_auto_changes(conn, cfg)
    return synthesized, bridged


def _team_digest_text(conn: psycopg.Connection, team_id: str, day: date) -> str:
    items = _items_for_day(conn, team_id, day)
    if not items:
        return ""
    lessons = [item for item in items if item["type"] == "lesson" and item["status"] == "gated"]
    changes = [item for item in items if item["type"] in _AUTO_TYPES and item["status"] == "gated"]
    infra = [item for item in items if item["status"] == "infra-notice"]
    quarantined = [item for item in items if item["status"] == "quarantined"]
    auto_count = sum(1 for item in changes if item["auto_queued"])
    lines = ["PM Retro Digest", f"Team: {team_id}", f"Date: {day}"]
    _add_section(lines, "Lessons learned", lessons)
    _add_section(lines, "Improvement candidates", changes)
    _add_section(lines, "Infra notices", infra)
    _add_section(lines, "Quarantined", quarantined)
    if auto_count:
        lines.extend(["", f"Auto-change PM requests queued: {auto_count}"])
    return "\n".join(lines).strip()


def _company_digest_text(conn: psycopg.Connection, day: date) -> str:
    items = _items_for_day(conn, COMPANY_TEAM_ID, day)
    if not items:
        return ""
    lessons = [item for item in items if item["type"] == "lesson" and item["status"] == "gated"]
    changes = [item for item in items if item["type"] in _AUTO_TYPES and item["status"] == "gated"]
    infra = [item for item in items if item["status"] == "infra-notice"]
    quarantined = [item for item in items if item["status"] == "quarantined"]
    auto_count = sum(1 for item in changes if item["auto_queued"])
    lines = ["CEO Retro Brief", f"Date: {day}"]
    _add_section(lines, "Company lessons learned", lessons)
    _add_section(lines, "Company improvement candidates", changes)
    _add_section(lines, "Company infra notices", infra)
    _add_section(lines, "Company quarantined", quarantined)
    if auto_count:
        lines.extend(["", f"Auto-change PM requests queued: {auto_count}"])
    return "\n".join(lines).strip()


def _items_for_day(conn: psycopg.Connection, team_id: str, day: date) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM retro_records WHERE team_id=%s AND retro_day=%s "
            "ORDER BY created_at",
            (team_id, day),
        )
        records = cur.fetchall()
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for (payload,) in records:
        for candidate in (payload or {}).get("candidates", []):
            item = _normalize_candidate(team_id, candidate)
            if item is None or item.id in seen:
                continue
            seen.add(item.id)
            items.append({
                "id": item.id,
                "team_id": item.team_id,
                "type": item.type,
                "status": item.status,
                "priority": item.priority,
                "statement": item.statement,
                "trigger": item.trigger,
                "auto_queued": False,
            })
    if not items:
        return []
    ids = [item["id"] for item in items]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, type, status, priority, statement, trigger, payload
            FROM retro_backlog
            WHERE id=ANY(%s)
            """,
            (ids,),
        )
        rows = {str(row[0]): row for row in cur.fetchall()}
    for item in items:
        row = rows.get(item["id"])
        if not row:
            continue
        _id, typ, status, priority, statement, trigger, payload = row
        item.update({
            "type": typ,
            "status": status,
            "priority": priority,
            "statement": statement,
            "trigger": trigger,
            "auto_queued": bool((payload or {}).get("auto_request_id")),
        })
    return sorted(items, key=lambda item: (-int(item["priority"]), item["statement"]))


def _add_section(lines: list[str], title: str, items: list[dict[str, Any]],
                 *, limit: int = 5) -> None:
    if not items:
        return
    lines.extend(["", f"{title}:"])
    for item in items[:limit]:
        trigger = f" ({item['trigger']})" if item.get("trigger") else ""
        lines.append(f"- [{item['type']}] {item['statement']}{trigger}")
    if len(items) > limit:
        lines.append(f"- ... {len(items) - limit} more")


def _queue_notify(conn: psycopg.Connection, cfg, *, team_id: str,
                  destination_ref: str | None, key: str, text: str) -> int:
    if not destination_ref:
        return 0
    try:
        cfg.team(team_id)
    except KeyError:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO actions
              (team_id, type, risk, destination_ref, idempotency_key, payload)
            VALUES (%s,'notify','reversible_internal',%s,%s,%s)
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            (team_id, destination_ref, key, Json({"text": text})),
        )
        return int(cur.rowcount)


def _control_destination(cfg, team_id: str) -> str | None:
    try:
        team = cfg.team(team_id)
    except KeyError:
        return None
    for channel in team.channels:
        if channel.role == "control" and channel.type != "cli":
            return f"{channel.type}:{channel.channel_id}"
    return None


def _normalize_candidate(team_id: str, candidate: dict) -> BacklogItem | None:
    typ = str(candidate.get("type") or "")
    statement = " ".join(str(candidate.get("statement") or "").split())
    trigger = " ".join(str(candidate.get("trigger") or "").split())
    if typ not in _TYPES or not statement:
        return None
    hits = scan(f"{statement} {trigger}")
    if hits:
        status = "quarantined"
    elif typ == "infra-flag":
        status = "infra-notice"
    else:
        status = "gated"
    priority = _priority(candidate.get("priority", candidate.get("impact")))
    raw = f"{team_id}:{typ}:{statement}:{trigger}"
    item_id = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return BacklogItem(item_id, team_id, typ, status, priority, statement, trigger)


def _priority(value) -> int:
    try:
        return max(1, min(5, int(value)))
    except (TypeError, ValueError):
        return 1


def _candidates_for_team(conn: psycopg.Connection, cfg, team_id: str,
                         retro_day: date) -> list[dict]:
    packet = _team_packet(conn, team_id, retro_day)
    agent_candidates = _agent_candidates(
        cfg, "facilitator.md",
        project=team_id, retro_day=retro_day, since=str(retro_day),
        until=str(retro_day), packet=packet,
    )
    if agent_candidates is not None:
        return agent_candidates
    return _deterministic_candidates(packet)


def _team_packet(conn: psycopg.Connection, team_id: str, retro_day: date) -> dict:
    return {
        "project": team_id,
        "date": str(retro_day),
        "pm_runs": _request_rows(conn, team_id, retro_day),
        "runs": _run_rows(conn, team_id, retro_day),
        "actions": _action_rows(conn, team_id, retro_day),
        "alerts": _alert_rows(conn, team_id, retro_day),
        "lessons": [lesson.__dict__ for lesson in pm_memory.selected(conn, team_id=team_id)],
        "backlog": [item.__dict__ for item in backlog(conn, team_id=team_id)],
    }


def _request_rows(conn: psycopg.Connection, team_id: str, retro_day: date) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id::text, r.fingerprint, r.status,
                   COALESCE(e.payload->>'text', e.payload->>'message',
                            e.payload->>'title', r.fingerprint, r.id::text),
                   r.updated_at::text
            FROM requests r
            JOIN events e ON e.id = r.event_id
            WHERE r.team_id=%s
              AND r.status IN ('done','failed')
              AND (r.updated_at AT TIME ZONE 'utc')::date=%s
            ORDER BY r.updated_at DESC
            LIMIT 50
            """,
            (team_id, retro_day),
        )
        rows = cur.fetchall()
    return [
        {"id": rid, "fingerprint": fp, "status": status, "text": text,
         "updated_at": updated_at}
        for rid, fp, status, text, updated_at in rows
    ]


def _run_rows(conn: psycopg.Connection, team_id: str, retro_day: date) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT runs.job_id::text, runs.role, runs.status,
                   left(COALESCE(runs.output, ''), 1000),
                   COALESCE(runs.ended_at, runs.started_at)::text
            FROM runs
            JOIN jobs ON jobs.id = runs.job_id
            WHERE jobs.team_id=%s
              AND (COALESCE(runs.ended_at, runs.started_at) AT TIME ZONE 'utc')::date=%s
            ORDER BY COALESCE(runs.ended_at, runs.started_at) DESC
            LIMIT 50
            """,
            (team_id, retro_day),
        )
        rows = cur.fetchall()
    return [
        {"job_id": job_id, "role": role, "status": status, "output": output,
         "at": at}
        for job_id, role, status, output, at in rows
    ]


def _action_rows(conn: psycopg.Connection, team_id: str, retro_day: date) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text, type, status, risk, provider_ref,
                   left(payload::text, 1000), updated_at::text
            FROM actions
            WHERE team_id=%s
              AND (updated_at AT TIME ZONE 'utc')::date=%s
            ORDER BY updated_at DESC
            LIMIT 50
            """,
            (team_id, retro_day),
        )
        rows = cur.fetchall()
    return [
        {"id": aid, "type": typ, "status": status, "risk": risk,
         "provider_ref": provider_ref, "payload": payload, "updated_at": updated_at}
        for aid, typ, status, risk, provider_ref, payload, updated_at in rows
    ]


def _alert_rows(conn: psycopg.Connection, team_id: str, retro_day: date) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text, severity, fingerprint, message, channel, ts::text
            FROM alerts
            WHERE project=%s AND (ts AT TIME ZONE 'utc')::date=%s
            ORDER BY ts DESC
            LIMIT 50
            """,
            (team_id, retro_day),
        )
        rows = cur.fetchall()
    return [
        {"id": aid, "severity": severity, "fingerprint": fingerprint,
         "message": message, "channel": channel, "ts": ts}
        for aid, severity, fingerprint, message, channel, ts in rows
    ]


def _deterministic_candidates(packet: dict) -> list[dict]:
    out = []
    for row in packet.get("pm_runs", [])[:20]:
        status = row.get("status")
        outcome = "failed" if status == "failed" else "passed"
        text = row.get("text") or row.get("fingerprint") or row.get("id") or ""
        fingerprint = row.get("fingerprint") or row.get("id") or text
        out.append({
            "type": "lesson",
            "priority": 3 if status == "failed" else 1,
            "statement": f"PM run {outcome}: {text}",
            "trigger": str(fingerprint or text or ""),
            "evidence_run_ids": [str(fingerprint)],
            "scope": f"project/{packet.get('project')}",
            "confidence": 0.6,
            "impact": 3 if status == "failed" else 1,
            "theme": f"pm-run-{outcome}",
        })
    return out


def _company_candidates_for_day(conn: psycopg.Connection, cfg, retro_day: date) -> list[dict]:
    packet = _company_packet(conn, retro_day)
    agent_candidates = _agent_candidates(
        cfg, "chief-of-staff.md",
        project="company", retro_day=retro_day, since=str(retro_day),
        until=str(retro_day), packet=packet,
    )
    if agent_candidates is not None:
        return agent_candidates
    return _deterministic_company_candidates(packet)


def _company_packet(conn: psycopg.Connection, retro_day: date) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT team_id, payload
            FROM retro_records
            WHERE retro_day=%s AND team_id<>%s
            ORDER BY created_at
            """,
            (retro_day, COMPANY_TEAM_ID),
        )
        records = cur.fetchall()
    teams = []
    for team_id, payload in records:
        teams.append({"team_id": team_id, "candidates": (payload or {}).get("candidates", [])})
    return {"date": str(retro_day), "teams": teams}


def _deterministic_company_candidates(packet: dict) -> list[dict]:
    grouped: dict[str, dict[str, Any]] = {}
    for team in packet.get("teams", []):
        team_id = str(team.get("team_id") or "")
        for candidate in team.get("candidates", []):
            theme = str(candidate.get("theme") or "").strip()
            if not theme:
                continue
            group = grouped.setdefault(theme, {
                "teams": set(), "evidence": set(), "statements": [],
                "types": [], "confidence": 0.0, "impact": 0,
            })
            group["teams"].add(team_id)
            group["statements"].append(str(candidate.get("statement") or ""))
            group["types"].append(str(candidate.get("type") or "lesson"))
            group["evidence"].update(_evidence_ids(candidate))
            group["confidence"] = max(group["confidence"], _float(candidate.get("confidence")))
            group["impact"] = max(group["impact"], int(_float(candidate.get("impact"))))
    out = []
    for theme, group in sorted(grouped.items()):
        teams = sorted(group["teams"])
        evidence = sorted(group["evidence"])
        if len(teams) < 2 and len(evidence) < 4:
            continue
        typ = next((t for t in group["types"] if t in _AUTO_TYPES), "lesson")
        first = next((s for s in group["statements"] if s), theme)
        out.append({
            "type": typ,
            "statement": f"Promote cross-team pattern {theme}: {first}",
            "trigger": f"{len(teams)} team(s), {len(evidence)} evidence id(s)",
            "evidence_run_ids": evidence,
            "source_team_ids": teams,
            "scope": "global",
            "confidence": max(group["confidence"], 0.7),
            "impact": max(group["impact"], 7),
            "theme": theme,
            "company_eligible": True,
        })
    return out


def _agent_candidates(cfg, prompt_name: str, *, project: str, retro_day: date,
                      since: str, until: str, packet: dict) -> list[dict] | None:
    engine = getattr(getattr(cfg.company, "defaults", None), "engine", None)
    if engine is None or engine.engine == "echo":
        return None
    prompt_path = _PROMPT_DIR / prompt_name
    if not prompt_path.is_file():
        return None
    prompt = prompt_path.read_text(encoding="utf-8")
    prompt = (prompt.replace("{{PROJECT}}", project)
                    .replace("{{DATE}}", str(retro_day))
                    .replace("{{SINCE}}", since)
                    .replace("{{UNTIL}}", until))
    prompt = f"{prompt}\n{json.dumps(packet, indent=2, default=str)}"
    try:
        from argus.engine import run_agent
        result = run_agent(engine.engine, prompt)
    except Exception:
        return None
    data = _parse_json_object(getattr(result, "text", ""))
    candidates = data.get("candidates") if isinstance(data, dict) else None
    if not isinstance(candidates, list):
        return None
    return [c for c in candidates if isinstance(c, dict)]


def _parse_json_object(text: str) -> dict | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(raw[start:end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None


def _bridge_company_lesson(conn: psycopg.Connection, cfg, *, item_id: str,
                           statement: str, trigger: str) -> None:
    source = f"retro:{item_id}"
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM knowledge WHERE scope='company' AND source=%s LIMIT 1",
                    (source,))
        if cur.fetchone():
            return
    knowledge_store.add(
        conn, cfg, scope="company", team_id=None,
        title="Company retro lesson",
        content=f"{statement}\nTrigger: {trigger}",
        source=source,
    )


def _enqueue_auto_changes(conn: psycopg.Connection, cfg) -> int:
    retro_cfg = getattr(cfg, "retro", None)
    if getattr(retro_cfg, "authority", "propose") != "auto-changes":
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, team_id, type, statement, trigger, payload
            FROM retro_backlog
            WHERE status='gated' AND type=ANY(%s)
            ORDER BY priority DESC, created_at
            """,
            (list(_AUTO_TYPES),),
        )
        rows = cur.fetchall()
    queued = 0
    for item_id, team_id, typ, statement, trigger, payload in rows:
        item_id = str(item_id)
        payload = payload or {}
        if payload.get("auto_request_id"):
            continue
        if not _auto_change_eligible(team_id, statement, trigger, payload):
            continue
        target_team = _auto_change_team(cfg, team_id)
        if not target_team:
            continue
        fingerprint = f"retro-change:{item_id}"
        existing = _request_by_fingerprint(conn, target_team, fingerprint)
        if existing:
            _mark_auto_queued(conn, item_id, existing, target_team)
            continue
        text = _auto_change_text(team_id=team_id, typ=typ, statement=statement,
                                 trigger=trigger, payload=payload)
        existing = _request_by_auto_change_task(conn, target_team=target_team,
                                                item_id=item_id,
                                                statement=statement,
                                                payload=payload)
        if existing:
            _mark_auto_queued(conn, item_id, existing, target_team)
            continue
        event_id = events.ingest_message(
            conn, cfg, team=target_team, source="retro",
            dedup_key=fingerprint, text=text,
            metadata={
                "retro_backlog_id": item_id,
                "retro_source_team": team_id,
                "retro_task_text": statement,
                "retro_theme": _payload_theme(payload),
            },
        )
        request_id = pipeline.open_request(
            conn, cfg, event_id=event_id, team_id=target_team,
            conversation_id=None, fingerprint=fingerprint,
        )
        if request_id:
            _mark_auto_queued(conn, item_id, request_id, target_team)
            queued += 1
    return queued


def _auto_change_eligible(team_id: str, statement: str, trigger: str,
                          payload: dict) -> bool:
    if scan(f"{statement} {trigger} {json.dumps(payload, default=str)}"):
        return False
    if _float(payload.get("confidence")) < 0.7:
        return False
    if _float(payload.get("impact")) < 7:
        return False
    evidence = _evidence_ids(payload)
    if len(evidence) < 3:
        return False
    if team_id == COMPANY_TEAM_ID:
        source_teams = {str(t) for t in payload.get("source_team_ids", []) if str(t)}
        if len(source_teams) < 2 and len(evidence) < 4:
            return False
    return True


def _auto_change_team(cfg, team_id: str) -> str | None:
    if team_id != COMPANY_TEAM_ID:
        return team_id
    configured = getattr(getattr(cfg, "retro", None), "company_change_team", None)
    if configured:
        return configured
    for team in cfg.teams:
        if getattr(team, "project", None):
            return team.name
    return cfg.teams[0].name if cfg.teams else None


def _request_by_fingerprint(conn: psycopg.Connection, team_id: str,
                            fingerprint: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id::text FROM requests WHERE team_id=%s AND fingerprint=%s LIMIT 1",
            (team_id, fingerprint),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _request_by_auto_change_task(conn: psycopg.Connection, *, target_team: str,
                                 item_id: str, statement: str,
                                 payload: dict) -> str | None:
    wanted = _auto_change_task_key(statement, payload)
    if wanted is None:
        return None
    existing = _request_by_retro_event_task(conn, target_team=target_team,
                                            wanted=wanted)
    if existing:
        return existing
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT payload->>'auto_request_id', statement, payload
            FROM retro_backlog
            WHERE id<>%s
              AND type=ANY(%s)
              AND payload ? 'auto_request_id'
              AND payload->>'auto_target_team'=%s
            ORDER BY updated_at DESC
            """,
            (item_id, list(_AUTO_TYPES), target_team),
        )
        rows = cur.fetchall()
    for request_id, existing_statement, existing_payload in rows:
        existing_payload = existing_payload or {}
        if _auto_change_task_key(str(existing_statement or ""), existing_payload) == wanted:
            return str(request_id)
    return None


def _request_by_retro_event_task(conn: psycopg.Connection, *, target_team: str,
                                 wanted: tuple[str, str]) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id::text, e.payload
            FROM requests r
            JOIN events e ON e.id = r.event_id
            WHERE r.team_id=%s
              AND e.source='retro'
              AND e.payload ? 'retro_task_text'
              AND e.payload ? 'retro_theme'
            ORDER BY r.updated_at DESC
            LIMIT 100
            """,
            (target_team,),
        )
        rows = cur.fetchall()
    for request_id, event_payload in rows:
        event_payload = event_payload or {}
        if _auto_change_task_key(str(event_payload.get("retro_task_text") or ""),
                                 event_payload) == wanted:
            return str(request_id)
    return None


def _auto_change_task_key(statement: str, payload: dict) -> tuple[str, str] | None:
    task_key = _task_key(statement)
    theme_key = _task_key(_payload_theme(payload))
    if not task_key or not theme_key:
        return None
    return task_key, theme_key


def _payload_theme(payload: dict) -> str:
    payload = payload or {}
    return str(payload.get("theme") or payload.get("retro_theme") or "").strip()


def _task_key(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split())


def _mark_auto_queued(conn: psycopg.Connection, item_id: str, request_id: str,
                      target_team: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE retro_backlog
            SET payload = payload || %s::jsonb, updated_at=clock_timestamp()
            WHERE id=%s
            """,
            (Json({
                "auto_request_id": request_id,
                "auto_target_team": target_team,
                "auto_queued_at": datetime.now(timezone.utc).isoformat(),
            }), item_id),
        )


def _auto_change_text(*, team_id: str, typ: str, statement: str,
                      trigger: str, payload: dict) -> str:
    scope = "company" if team_id == COMPANY_TEAM_ID else f"team {team_id}"
    evidence = ", ".join(_evidence_ids(payload)) or "none"
    return "\n".join([
        f"Retro auto-change request for {scope}.",
        f"Type: {typ}",
        f"Change: {statement}",
        f"Trigger: {trigger}",
        f"Evidence: {evidence}",
        "Implement minimal internal change. Do not merge, deploy, send messages, "
        "or change secrets.",
    ])


def _evidence_ids(candidate: dict) -> set[str]:
    raw = (candidate.get("evidence_run_ids")
           or candidate.get("evidence_ids")
           or candidate.get("evidence")
           or [])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return set()
    return {str(item) for item in raw if str(item)}


def _float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
