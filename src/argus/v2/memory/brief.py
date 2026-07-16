"""Pure database projection for one team's project memory brief."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from argus.v2.context.sanitize import sanitize_memory

_PROMPT_CAP = 3_000
_ITEM_LIMIT = 10
_TEXT_LIMIT = 500
_PROMPT_ITEM_LIMIT = 160
_EVIDENCE = re.compile(r"^(event|request|action):([0-9a-fA-F-]{36})$")


@dataclass(frozen=True)
class BriefItem:
    text: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProjectMemoryBrief:
    team_id: str
    generated_at: str
    current_work: tuple[BriefItem, ...] = ()
    open_loops: tuple[BriefItem, ...] = ()
    recent_decisions: tuple[BriefItem, ...] = ()
    validated_lessons: tuple[BriefItem, ...] = ()
    recent_outcomes: tuple[BriefItem, ...] = ()
    pending_retro: tuple[BriefItem, ...] = ()

    def as_dict(self) -> dict:
        return asdict(self)


def build(conn, cfg, team_id: str, now: datetime) -> ProjectMemoryBrief:
    now = now.astimezone(timezone.utc)
    since_day = (now - timedelta(days=14)).date()
    current_work: list[BriefItem] = []
    open_loops: list[BriefItem] = []
    decisions: list[BriefItem] = []
    lessons: list[BriefItem] = []
    outcomes: list[BriefItem] = []
    pending: list[BriefItem] = []

    with conn.cursor() as cur:
        cur.execute(
            """SELECT r.id, r.status, COALESCE(e.payload->>'text', '')
               FROM requests r
               LEFT JOIN events e ON e.id=r.event_id AND e.team_id=%s
               WHERE r.team_id=%s AND r.status IN ('open','awaiting_approval')
               ORDER BY r.created_at DESC, r.id DESC
               LIMIT %s""",
            (team_id, team_id, _ITEM_LIMIT),
        )
        for request_id, status, text in cur.fetchall():
            label = "approval wait" if status == "awaiting_approval" else "open"
            current_work.append(
                _item(f"{request_id} ({label}): {text}", (f"request:{request_id}",))
            )
        cur.execute(
            """SELECT id, type, destination_ref
               FROM actions
               WHERE team_id=%s AND status='awaiting_approval'
               ORDER BY created_at DESC, id DESC
               LIMIT %s""",
            (team_id, _ITEM_LIMIT),
        )
        for action_id, action_type, destination in cur.fetchall():
            suffix = f" for {destination}" if destination else ""
            current_work.append(
                _item(
                    f"Approval wait: {action_type}{suffix}",
                    (f"action:{action_id}",),
                )
            )
        cur.execute(
            """SELECT details
               FROM conversation_summaries
               WHERE team_id=%s AND day >= %s
               ORDER BY day DESC, updated_at DESC, id DESC
               LIMIT 30""",
            (team_id, since_day),
        )
        summary_rows = cur.fetchall()
        cur.execute(
            """WITH latest AS (
                 SELECT DISTINCT ON (fingerprint)
                        fingerprint, finding, outcome, note, created_at, id
                 FROM pm_lessons
                 WHERE team_id=%s
                 ORDER BY fingerprint, created_at DESC, id DESC
               )
               SELECT finding, outcome, note
               FROM latest
               WHERE outcome IN ('qa-pass','manual-qa')
               ORDER BY created_at DESC, id DESC
               LIMIT %s""",
            (team_id, _ITEM_LIMIT),
        )
        for finding, outcome, note in cur.fetchall():
            suffix = f" ({note})" if note else ""
            lessons.append(_item(f"{outcome}: {finding}{suffix}"))
        cur.execute(
            """SELECT title, content
               FROM knowledge
               WHERE scope='company' OR (scope='team' AND team_id=%s)
               ORDER BY created_at DESC, id DESC
               LIMIT %s""",
            (team_id, _ITEM_LIMIT),
        )
        for title, content in cur.fetchall():
            lessons.append(_item(f"{title}: {content}"))
        cur.execute(
            """SELECT id, provider_ref
               FROM actions
               WHERE team_id=%s AND type IN ('open_pr','pr')
                 AND status='done' AND provider_ref LIKE 'http%%'
               ORDER BY created_at DESC, id DESC
               LIMIT %s""",
            (team_id, _ITEM_LIMIT),
        )
        for action_id, provider_ref in cur.fetchall():
            outcomes.append(
                _item(f"Argus-created PR: {provider_ref}", (f"action:{action_id}",))
            )
        cur.execute(
            """SELECT id, type, status, statement
               FROM retro_backlog
               WHERE team_id=%s
               ORDER BY priority DESC, created_at, id
               LIMIT %s""",
            (team_id, _ITEM_LIMIT),
        )
        for item_id, item_type, status, statement in cur.fetchall():
            pending.append(_item(f"[{status}/{item_type}] {statement} ({item_id})"))

    for (details,) in summary_rows:
        body = details or {}
        _extend_from_details(open_loops, body.get("open_loops"), conn, team_id)
        _extend_from_details(decisions, body.get("decisions"), conn, team_id)
        _extend_from_details(outcomes, body.get("outcomes"), conn, team_id)

    return ProjectMemoryBrief(
        team_id=team_id,
        generated_at=now.isoformat(),
        current_work=tuple(_dedupe(current_work)[:_ITEM_LIMIT]),
        open_loops=tuple(_dedupe(open_loops)[:_ITEM_LIMIT]),
        recent_decisions=tuple(_dedupe(decisions)[:_ITEM_LIMIT]),
        validated_lessons=tuple(_dedupe(lessons)[:_ITEM_LIMIT]),
        recent_outcomes=tuple(_dedupe(outcomes)[:_ITEM_LIMIT]),
        pending_retro=tuple(_dedupe(pending)[:_ITEM_LIMIT]),
    )


def render_text(brief: ProjectMemoryBrief) -> str:
    sections = _section_values(brief)
    lines = [f"Project memory brief: {brief.team_id}"]
    for title, items in sections:
        lines.extend(["", f"{title}:"])
        if items:
            lines.extend(f"- {item.text}" for item in items)
        else:
            lines.append("- none")
    return "\n".join(lines)


def render_json(brief: ProjectMemoryBrief) -> str:
    return json.dumps(brief.as_dict(), sort_keys=True, separators=(",", ":"))


def render_prompt(brief: ProjectMemoryBrief) -> str:
    values = {
        "Current work and approval waits": list(brief.current_work[:5]),
        "Open loops": list(brief.open_loops[:5]),
        "Recent decisions": list(brief.recent_decisions[:5]),
        "Validated PM lessons": list(brief.validated_lessons[:5]),
        "Recent outcomes and Argus-created PRs": list(brief.recent_outcomes[:5]),
        "Pending retro items": list(brief.pending_retro[:5]),
    }
    optional_drop_order = [
        "Pending retro items",
        "Recent outcomes and Argus-created PRs",
        "Validated PM lessons",
    ]
    while len(_prompt_text(brief.team_id, values)) > _PROMPT_CAP:
        dropped = False
        for title in optional_drop_order:
            if values[title]:
                values[title].pop()
                dropped = True
                break
        if dropped:
            continue
        if len(values["Recent decisions"]) > 1:
            values["Recent decisions"].pop()
            continue
        if len(values["Open loops"]) > 1:
            values["Open loops"].pop()
            continue
        if len(values["Current work and approval waits"]) > 1:
            values["Current work and approval waits"].pop()
            continue
        break
    return _prompt_text(brief.team_id, values)[:_PROMPT_CAP]


def _prompt_text(team_id: str, values: dict[str, list[BriefItem]]) -> str:
    lines = [f"--- PROJECT MEMORY: {team_id} ---"]
    for title, items in values.items():
        lines.append(f"{title}:")
        if items:
            lines.extend(f"- {item.text[:_PROMPT_ITEM_LIMIT]}" for item in items)
        else:
            lines.append("- none")
    lines.append("--- END PROJECT MEMORY ---")
    return "\n".join(lines)


def _section_values(brief: ProjectMemoryBrief):
    return (
        ("Current work and approval waits", brief.current_work),
        ("Open loops", brief.open_loops),
        ("Recent decisions", brief.recent_decisions),
        ("Validated PM lessons", brief.validated_lessons),
        ("Recent outcomes and Argus-created PRs", brief.recent_outcomes),
        ("Pending retro items", brief.pending_retro),
    )


def _item(text: str, evidence_ids: Iterable[str] = ()) -> BriefItem:
    return BriefItem(
        text=sanitize_memory(text)[:_TEXT_LIMIT],
        evidence_ids=tuple(str(value) for value in evidence_ids),
    )


def _extend_from_details(target, raw_items, conn, team_id: str) -> None:
    if not isinstance(raw_items, list):
        return
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        evidence = raw.get("evidence_ids") or []
        valid = _resolvable_evidence(conn, team_id, evidence)
        if evidence and len(valid) != len(evidence):
            continue
        text = raw.get("text")
        if text:
            target.append(_item(str(text), valid))


def _resolvable_evidence(conn, team_id: str, evidence_ids: Iterable[str]) -> tuple[str, ...]:
    valid: list[str] = []
    for value in evidence_ids:
        value = str(value)
        match = _EVIDENCE.fullmatch(value)
        if not match:
            continue
        table, row_id = match.groups()
        table_name = {"event": "events", "request": "requests", "action": "actions"}[table]
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT 1 FROM {table_name} WHERE id=%s AND team_id=%s",
                (row_id, team_id),
            )
            if cur.fetchone():
                valid.append(value)
    return tuple(valid)


def _dedupe(items: Iterable[BriefItem]) -> list[BriefItem]:
    seen: set[str] = set()
    result: list[BriefItem] = []
    for item in items:
        key = " ".join(item.text.casefold().split())
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
