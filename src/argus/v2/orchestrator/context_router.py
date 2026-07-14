"""Durable follow-up routing for owner conversations.

Conversation contexts sit in front of the generic front/manager fallback. They
let a later owner message like "what did she request?" attach to the support
case that just notified the group instead of becoming an unrelated chat turn.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.types.json import Json

from argus.v2.orchestrator import pipeline


def register_context(conn: psycopg.Connection, *, team_id: str, channel_ref: str,
                     context_type: str, context_ref: str, summary: str = "",
                     payload: dict[str, Any] | None = None,
                     ttl_hours: int = 72) -> str:
    expires = datetime.now(timezone.utc) + timedelta(hours=max(ttl_hours, 1))
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO conversation_contexts (
              team_id, channel_ref, context_type, context_ref, status,
              summary, payload, expires_at)
            VALUES (%s,%s,%s,%s,'active',%s,%s,%s)
            ON CONFLICT (team_id, channel_ref, context_type, context_ref)
            DO UPDATE SET status='active',
                          summary=excluded.summary,
                          payload=excluded.payload,
                          expires_at=excluded.expires_at,
                          updated_at=now()
            RETURNING id
            """,
            (team_id, channel_ref, context_type, context_ref, summary,
             Json(payload or {}), expires),
        )
        return str(cur.fetchone()[0])


def active_context(conn: psycopg.Connection, *, team_id: str,
                   channel_ref: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE conversation_contexts
               SET status='expired', updated_at=now()
             WHERE team_id=%s AND channel_ref=%s AND status='active'
               AND expires_at IS NOT NULL AND expires_at <= now()
            """,
            (team_id, channel_ref),
        )
        cur.execute(
            """
            SELECT id, team_id, channel_ref, context_type, context_ref,
                   status, summary, payload
              FROM conversation_contexts
             WHERE team_id=%s AND channel_ref=%s AND status='active'
               AND (expires_at IS NULL OR expires_at > now())
             ORDER BY updated_at DESC
             LIMIT 1
            """,
            (team_id, channel_ref),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "id": str(row[0]),
        "team_id": row[1],
        "channel_ref": row[2],
        "context_type": row[3],
        "context_ref": row[4],
        "status": row[5],
        "summary": row[6],
        "payload": row[7] or {},
    }


def resolve_context(conn: psycopg.Connection, *, context_id: str,
                    status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE conversation_contexts SET status=%s, updated_at=now() WHERE id=%s",
            (status, context_id),
        )


def touch_context(conn: psycopg.Connection, *, context_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE conversation_contexts SET updated_at=now() WHERE id=%s",
                    (context_id,))


def handle_message(conn: psycopg.Connection, cfg, *, team_id: str,
                   channel_ref: str, event_id: str, text: str) -> bool:
    context = active_context(conn, team_id=team_id, channel_ref=channel_ref)
    if not context:
        return False
    if context["context_type"] == "branch_drift":
        return _handle_branch_drift(
            conn, cfg, team_id=team_id, channel_ref=channel_ref,
            event_id=event_id, context=context, text=text)
    if context["context_type"] == "system_health":
        return _handle_system_health(
            conn, cfg, team_id=team_id, channel_ref=channel_ref,
            event_id=event_id, context=context, text=text)
    if context["context_type"] != "support_case":
        return False

    from argus.v2.support import cycle as support_cycle
    result = support_cycle.handle_context_message(
        conn, cfg, team_id=team_id, context=context, text=text)
    if not result.handled:
        _attach_support_context(conn, event_id=event_id, context=context)
        return False

    if result.context_status:
        resolve_context(conn, context_id=context["id"], status=result.context_status)
    else:
        touch_context(conn, context_id=context["id"])
    if result.reply:
        _emit_reply(conn, team_id=team_id, channel_ref=channel_ref,
                    event_id=event_id, text=result.reply)
    return True


def _attach_support_context(conn: psycopg.Connection, *, event_id: str,
                            context: dict[str, Any]) -> None:
    payload = context.get("payload") or {}
    support_context = {
        "context_id": context.get("id"),
        "context_ref": context.get("context_ref"),
        "channel_ref": context.get("channel_ref"),
        "summary": context.get("summary") or "",
        "sender": payload.get("from") or payload.get("sender") or "",
        "subject": payload.get("subject") or "",
        "question": payload.get("question") or "",
        "proposed_reply": payload.get("proposed_reply") or "",
        "customer_request": str(payload.get("thread") or "")[:4000],
    }
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE events SET payload=jsonb_set(payload, '{support_context}', %s::jsonb) "
            "WHERE id=%s",
            (Json(support_context), event_id),
        )


def _handle_system_health(conn: psycopg.Connection, cfg, *, team_id: str,
                          channel_ref: str, event_id: str,
                          context: dict[str, Any], text: str) -> bool:
    if not _approves_fix(text):
        return False
    task = _system_health_task(context=context)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE events SET payload=jsonb_set(payload, '{text}', %s::jsonb) "
            "WHERE id=%s",
            (Json(task), event_id),
        )
    request_id = pipeline.open_request(
        conn, cfg, event_id=event_id, team_id=team_id, conversation_id=None,
        fingerprint=context["context_ref"])
    resolve_context(conn, context_id=context["id"], status="resolved")
    reply = "On it, I'll fix the latest Argus health issue."
    if request_id is None:
        reply = "Already working on that Argus health issue."
    _emit_reply(conn, team_id=team_id, channel_ref=channel_ref,
                event_id=event_id, text=reply)
    return True


def _handle_branch_drift(conn: psycopg.Connection, cfg, *, team_id: str,
                         channel_ref: str, event_id: str, context: dict[str, Any],
                         text: str) -> bool:
    if not _approves_branch_sync(text):
        return False
    task = _branch_sync_task(team_id=team_id, context=context)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE events SET payload=jsonb_set(payload, '{text}', %s::jsonb) "
            "WHERE id=%s",
            (Json(task), event_id),
        )
    request_id = pipeline.open_request(
        conn, cfg, event_id=event_id, team_id=team_id, conversation_id=None,
        fingerprint=context["context_ref"])
    resolve_context(conn, context_id=context["id"], status="resolved")
    reply = "On it, I'll run the branch-sync PR flow."
    if request_id is None:
        reply = "Already working on that branch sync."
    _emit_reply(conn, team_id=team_id, channel_ref=channel_ref,
                event_id=event_id, text=reply)
    return True


def _approves_branch_sync(text: str) -> bool:
    cleaned = " ".join((text or "").strip().lower().split())
    return cleaned in {
        "do it",
        "go",
        "go ahead",
        "ok",
        "okay",
        "yes",
        "approve",
        "approved",
        "sync",
        "sync it",
        "run it",
        "תעשה",
        "תעשה את זה",
        "כן",
        "יאללה",
        "קדימה",
        "סנכרן",
        "תסנכרן",
    }


def _approves_fix(text: str) -> bool:
    cleaned = " ".join((text or "").strip().lower().split())
    return cleaned in {
        "fix",
        "fix it",
        "do it",
        "go",
        "go ahead",
        "ok",
        "okay",
        "yes",
        "approve",
        "approved",
        "run it",
        "תתקן",
        "תקן",
        "תעשה",
        "תעשה את זה",
        "כן",
        "יאללה",
        "קדימה",
    }


def _system_health_task(*, context: dict[str, Any]) -> str:
    payload = context.get("payload") or {}
    findings = payload.get("findings") or []
    if findings:
        details = "; ".join(
            str(item.get("message") or item.get("fingerprint") or "")
            for item in findings[:8]
            if isinstance(item, dict)
        )
    else:
        details = context.get("summary") or "Argus health issue"
    return (
        "Owner asked to fix latest Argus health issue from the general group. "
        f"Health finding: {details}. "
        "Start from live state: launchctl, Argus logs, DB outbox, and runtime "
        "env/config as relevant. If the issue is still real, make the smallest "
        "repo or runtime fix needed and verify end to end. Do not merge, deploy, "
        "spend money, or weaken auth boundaries. If the issue is already fixed, "
        "report the current proof instead of changing code."
    )


def _branch_sync_task(*, team_id: str, context: dict[str, Any]) -> str:
    payload = context.get("payload") or {}
    summary = context.get("summary") or payload.get("message") or "branch drift detected"
    base = payload.get("base") or "main"
    head = payload.get("head") or "current branch"
    return (
        f"Owner approved branch drift sync for {team_id}: {summary}. "
        f"Compare origin/{base} and origin/{head}. Prepare the minimal safe "
        f"branch-sync PR to reconcile {head} with {base}. Do not merge, deploy, "
        f"or force-push. Preserve legitimate branch-only commits. If syncing "
        f"would drop changes, create conflicts, or require choosing a release "
        f"direction, stop and report the exact blocker. Use git and gh as "
        f"needed for branch maintenance, then run the narrowest relevant checks."
    )


def _emit_reply(conn: psycopg.Connection, *, team_id: str, channel_ref: str,
                event_id: str, text: str) -> None:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO actions (team_id, type, risk, destination_ref,
                                    idempotency_key, payload)
               VALUES (%s,'reply','reversible_internal',%s,%s,%s)
               ON CONFLICT (idempotency_key) DO NOTHING""",
            (team_id, channel_ref, f"context-reply:{event_id}:{digest}",
             Json({"text": text})),
        )
