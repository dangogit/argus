"""Secure semantic daily summaries for durable project memory."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from psycopg.types.json import Json

from argus.engine import EngineOutageError
from argus.v2.context import engine
from argus.v2.context.sanitize import sanitize_memory

_CHUNK_CHARS = 12_000
_MAX_CHUNKS = 8
_MAX_SOURCE_TEXT = 1_000
_MAX_ITEMS = 20
_MAX_ITEM_TEXT = 500
_MAX_EVIDENCE = 10
_EVIDENCE = re.compile(r"^(event|request|action):([0-9a-fA-F-]{36})$")
_SECTIONS = ("decisions", "open_loops", "outcomes")


@dataclass(frozen=True)
class SummaryRefreshResult:
    status: str
    summary: str
    details: dict[str, Any]
    source_fingerprint: str
    message_count: int


@dataclass(frozen=True)
class _Source:
    at: str
    evidence_id: str
    kind: str
    text: str
    fingerprint_value: Any

    def line(self) -> str:
        clean = sanitize_memory(self.text)[:_MAX_SOURCE_TEXT]
        return f"{self.at} {self.evidence_id} [{self.kind}] {clean}".strip()


def refresh_day(
    conn,
    cfg,
    *,
    team_id: str,
    conversation_id,
    day: date,
    engine_runner=None,
) -> Optional[SummaryRefreshResult]:
    sources, counts = _load_sources(
        conn, team_id=team_id, conversation_id=conversation_id, day=day
    )
    if not sources:
        return None

    fingerprint = _fingerprint(sources)
    existing = _existing(conn, team_id, conversation_id, day)
    if (
        existing
        and existing[2] == fingerprint
        and (existing[1] or {}).get("status") == "semantic"
    ):
        details = dict(existing[1])
        return SummaryRefreshResult(
            status="unchanged",
            summary=existing[0],
            details=details,
            source_fingerprint=fingerprint,
            message_count=counts["messages"],
        )

    chunks, truncated = _chunks(sources)
    allowed = {source.evidence_id for source in sources}
    merged = {section: [] for section in _SECTIONS}
    seen = {section: set() for section in _SECTIONS}
    failed_chunks = 0
    valid_chunks = 0

    for chunk in chunks:
        try:
            raw = engine.call("daily_summary", chunk, engine_runner=engine_runner)
            parsed = _parse_chunk(raw, allowed)
        except (EngineOutageError, json.JSONDecodeError, TypeError, ValueError):
            failed_chunks += 1
            continue
        valid_chunks += 1
        for section in _SECTIONS:
            for item in parsed[section]:
                normalized = " ".join(item["text"].casefold().split())
                if normalized in seen[section]:
                    continue
                seen[section].add(normalized)
                merged[section].append(item)

    if valid_chunks == 0:
        status = "fallback"
        merged = {section: [] for section in _SECTIONS}
    elif failed_chunks:
        status = "partial"
    else:
        status = "semantic"

    details = {
        "version": 1,
        "status": status,
        **merged,
        "chunk_count": len(chunks),
        "failed_chunks": failed_chunks,
        "truncated": truncated,
    }
    summary = _render_summary(details, counts)
    result = SummaryRefreshResult(
        status=status,
        summary=summary,
        details=details,
        source_fingerprint=fingerprint,
        message_count=counts["messages"],
    )
    _store(conn, team_id, conversation_id, day, result, existing)
    return result


def summarize_day(conn, cfg, *, team_id: str, conversation_id, day: date) -> Optional[str]:
    """Compatibility command returning only the summary text."""
    result = refresh_day(
        conn,
        cfg,
        team_id=team_id,
        conversation_id=conversation_id,
        day=day,
    )
    return result.summary if result else None


def _load_sources(conn, *, team_id: str, conversation_id, day: date):
    sources: list[_Source] = []
    counts = {"messages": 0, "requests": 0, "actions": 0}
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, received_at, COALESCE(payload->>'text', '')
               FROM events
               WHERE team_id=%s AND kind='message'
                 AND (%s::uuid IS NULL OR conversation_id=%s::uuid)
                 AND (received_at AT TIME ZONE 'utc')::date=%s
               ORDER BY received_at, id""",
            (team_id, conversation_id, conversation_id, day),
        )
        for row_id, at, text in cur.fetchall():
            counts["messages"] += 1
            sources.append(
                _Source(
                    at=at.isoformat(),
                    evidence_id=f"event:{row_id}",
                    kind="message",
                    text=text or "",
                    fingerprint_value=text or "",
                )
            )
        cur.execute(
            """SELECT r.id, r.created_at, r.status,
                      COALESCE(e.payload->>'text', '')
               FROM requests r
               LEFT JOIN events e ON e.id=r.event_id
               WHERE r.team_id=%s
                 AND (r.created_at AT TIME ZONE 'utc')::date=%s
               ORDER BY r.created_at, r.id""",
            (team_id, day),
        )
        for row_id, at, status, text in cur.fetchall():
            counts["requests"] += 1
            value = f"status={status} {text or ''}".strip()
            sources.append(
                _Source(
                    at=at.isoformat(),
                    evidence_id=f"request:{row_id}",
                    kind="request",
                    text=value,
                    fingerprint_value=value,
                )
            )
        cur.execute(
            """SELECT id, created_at, type, status, provider_ref, payload
               FROM actions
               WHERE team_id=%s
                 AND (created_at AT TIME ZONE 'utc')::date=%s
               ORDER BY created_at, id""",
            (team_id, day),
        )
        for row_id, at, action_type, status, provider_ref, payload in cur.fetchall():
            counts["actions"] += 1
            value = json.dumps(
                {
                    "type": action_type,
                    "status": status,
                    "provider_ref": provider_ref,
                    "payload": payload or {},
                },
                sort_keys=True,
                default=str,
            )
            sources.append(
                _Source(
                    at=at.isoformat(),
                    evidence_id=f"action:{row_id}",
                    kind="action",
                    text=value,
                    fingerprint_value=value,
                )
            )
    sources.sort(key=lambda source: (source.at, source.evidence_id))
    return sources, counts


def _fingerprint(sources: list[_Source]) -> str:
    canonical = [
        [source.at, source.evidence_id, source.kind, source.fingerprint_value]
        for source in sources
    ]
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _chunks(sources: list[_Source]) -> tuple[list[str], bool]:
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for source in sources:
        line = source.line()
        added = len(line) + (1 if current else 0)
        if current and size + added > _CHUNK_CHARS:
            chunks.append(current)
            current = []
            size = 0
            added = len(line)
        current.append(line)
        size += added
    if current:
        chunks.append(current)
    truncated = len(chunks) > _MAX_CHUNKS
    if truncated:
        chunks = chunks[:4] + chunks[-4:]
    return ["\n".join(chunk) for chunk in chunks], truncated


def _parse_chunk(raw: str, allowed: set[str]) -> dict[str, list[dict[str, Any]]]:
    data = json.loads(raw)
    if not isinstance(data, dict) or any(not isinstance(data.get(key), list) for key in _SECTIONS):
        raise ValueError("daily summary must contain three lists")
    parsed: dict[str, list[dict[str, Any]]] = {section: [] for section in _SECTIONS}
    rejected = 0
    supplied = 0
    for section in _SECTIONS:
        for item in data[section][:_MAX_ITEMS]:
            supplied += 1
            if not isinstance(item, dict):
                rejected += 1
                continue
            text = sanitize_memory(str(item.get("text", "")))[:_MAX_ITEM_TEXT].strip()
            evidence = item.get("evidence_ids")
            if not text or not isinstance(evidence, list) or not evidence:
                rejected += 1
                continue
            evidence_ids = []
            for value in evidence[:_MAX_EVIDENCE]:
                value = str(value)
                if _EVIDENCE.fullmatch(value) and value in allowed and value not in evidence_ids:
                    evidence_ids.append(value)
            if not evidence_ids or len(evidence_ids) != len(evidence[:_MAX_EVIDENCE]):
                rejected += 1
                continue
            parsed[section].append({"text": text, "evidence_ids": evidence_ids})
    if supplied and rejected == supplied:
        raise ValueError("daily summary contained no valid items")
    return parsed


def _render_summary(details: dict[str, Any], counts: dict[str, int]) -> str:
    lines = [
        f"{counts['messages']} message(s); {counts['requests']} request(s) opened; "
        f"{counts['actions']} action(s) recorded."
    ]
    labels = {"decisions": "Decisions", "open_loops": "Open loops", "outcomes": "Outcomes"}
    for section in _SECTIONS:
        items = details[section]
        if not items:
            continue
        lines.append(f"{labels[section]}:")
        lines.extend(f"- {item['text']}" for item in items)
    return "\n".join(lines)


def _existing(conn, team_id: str, conversation_id, day: date):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT summary, details, source_fingerprint, id
               FROM conversation_summaries
               WHERE team_id=%s AND day=%s
                 AND ((%s::uuid IS NULL AND conversation_id IS NULL) OR conversation_id=%s::uuid)""",
            (team_id, day, conversation_id, conversation_id),
        )
        return cur.fetchone()


def _store(conn, team_id, conversation_id, day, result, existing) -> None:
    if (
        existing
        and existing[2] == result.source_fingerprint
        and (existing[1] or {}).get("status") == "semantic"
        and result.status != "semantic"
    ):
        return
    with conn.cursor() as cur:
        if existing:
            cur.execute(
                """UPDATE conversation_summaries
                   SET summary=%s, message_count=%s, details=%s,
                       source_fingerprint=%s, updated_at=now()
                   WHERE id=%s""",
                (
                    result.summary,
                    result.message_count,
                    Json(result.details),
                    result.source_fingerprint,
                    existing[3],
                ),
            )
        else:
            cur.execute(
                """INSERT INTO conversation_summaries
                   (team_id, conversation_id, day, summary, message_count,
                    details, source_fingerprint)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (
                    team_id,
                    conversation_id,
                    day,
                    result.summary,
                    result.message_count,
                    Json(result.details),
                    result.source_fingerprint,
                ),
            )
