"""Typed human interactions: ask / confirm / suggest.

One primitive over the existing conversation_contexts + actions outbox (no new
tables). An interaction posts an idempotent prompt to a channel and registers a
durable context; the owner's next matching reply on that channel resolves it:

- ``confirm``: yes/no on a pending approval. Reply "approve"/"reject" in chat
  consumes the approval nonce - no CLI round-trip. Anything else falls through
  to normal conversation and the interaction stays pending.
- ``ask``: a blocked pipeline asks for guidance. The owner's next reply becomes
  the guidance and reopens the request with it; dismissal phrases drop it.
- ``suggest``: a non-blocking proposal ("sync the branches?"). A yes-phrase
  opens the prepared request; a no-phrase dismisses; anything else falls
  through.

All interception is scoped to the one active context on that channel and
TTL-bound, so a stale interaction can never hijack chat forever.
"""
from __future__ import annotations

import logging

import psycopg
from psycopg.types.json import Json

log = logging.getLogger(__name__)

KINDS = ("ask", "confirm", "suggest")

# Shared english+hebrew phrase sets. Callers can extend yes per interaction
# (e.g. branch sync accepts "sync it") via payload["extra_yes"].
YES_PHRASES = frozenset({
    "yes", "ok", "okay", "go", "go ahead", "do it", "run it", "fix", "fix it",
    "approve", "approved", "sure", "yep",
    "כן", "יאללה", "קדימה", "תעשה", "תעשה את זה", "תתקן", "תקן", "אשר", "מאשר",
})
NO_PHRASES = frozenset({
    "no", "nope", "reject", "rejected", "cancel", "stop", "skip", "not now",
    "later", "leave it", "dont", "don't",
    "לא", "עזוב", "אל", "לא עכשיו", "דחה", "בטל",
})


def _clean(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def is_yes(text: str, extra=()) -> bool:
    return _clean(text) in YES_PHRASES | frozenset(extra)


def is_no(text: str) -> bool:
    return _clean(text) in NO_PHRASES


def control_channel(cfg, team_id: str) -> str | None:
    """The team's non-cli control channel ref, or None."""
    try:
        team = cfg.team(team_id)
    except KeyError:
        return None
    for ch in getattr(team, "channels", []) or []:
        if ch.role == "control" and ch.type != "cli":
            return f"{ch.type}:{ch.channel_id}"
    return None


def open_interaction(conn: psycopg.Connection, *, team_id: str, channel_ref: str,
                     kind: str, key: str, prompt: str | None = None,
                     payload: dict | None = None, summary: str = "",
                     ttl_hours: int = 24) -> None:
    """Post the prompt (idempotent per key; skipped when prompt is None because
    the caller already sent its own message) and register the pending
    interaction context. Re-opening the same key refreshes the context."""
    assert kind in KINDS, kind
    if prompt:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO actions (team_id, type, risk, destination_ref, "
                "  idempotency_key, payload) "
                "VALUES (%s,'notify','reversible_internal',%s,%s,%s) "
                "ON CONFLICT (idempotency_key) DO NOTHING",
                (team_id, channel_ref, f"interaction:{key}", Json({"text": prompt})))
    from argus.v2.orchestrator import context_router
    context_router.register_context(
        conn, team_id=team_id, channel_ref=channel_ref,
        context_type="interaction", context_ref=key,
        summary=summary or (prompt or "")[:200],
        payload={"kind": kind, **(payload or {})}, ttl_hours=ttl_hours)


def handle_reply(conn: psycopg.Connection, cfg, *, team_id: str, channel_ref: str,
                 event_id: str, context: dict, text: str) -> bool:
    """Route an owner reply at the active interaction. Returns False (context
    kept) when the reply doesn't address it, so normal chat falls through."""
    payload = dict(context.get("payload") or {})
    kind = payload.get("kind")
    if kind == "confirm":
        return _handle_confirm(conn, team_id=team_id, channel_ref=channel_ref,
                               event_id=event_id, context=context,
                               payload=payload, text=text)
    if kind == "suggest":
        return _handle_suggest(conn, cfg, team_id=team_id, channel_ref=channel_ref,
                               event_id=event_id, context=context,
                               payload=payload, text=text)
    if kind == "ask":
        return _handle_ask(conn, cfg, team_id=team_id, channel_ref=channel_ref,
                           event_id=event_id, context=context,
                           payload=payload, text=text)
    log.warning("unknown interaction kind %r for context %s", kind, context.get("id"))
    return False


def _handle_confirm(conn, *, team_id, channel_ref, event_id, context, payload,
                    text) -> bool:
    if is_yes(text):
        decision = "approved"
    elif is_no(text):
        decision = "rejected"
    else:
        return False
    from argus.v2.actions import approvals
    from argus.v2.orchestrator import context_router
    ok = approvals.consume(conn, str(payload.get("nonce") or ""),
                           decision=decision, approver_ref=channel_ref)
    context_router.resolve_context(conn, context_id=context["id"], status="resolved")
    if not ok:
        reply = "That approval is no longer pending (already decided or expired)."
    elif decision == "approved":
        reply = "✅ Approved - running it."
    else:
        reply = "❌ Rejected - I won't run it."
    context_router._emit_reply(conn, team_id=team_id, channel_ref=channel_ref,
                               event_id=event_id, text=reply)
    return True


def _handle_suggest(conn, cfg, *, team_id, channel_ref, event_id, context,
                    payload, text) -> bool:
    from argus.v2.orchestrator import context_router
    if is_no(text):
        context_router.resolve_context(conn, context_id=context["id"], status="expired")
        context_router._emit_reply(conn, team_id=team_id, channel_ref=channel_ref,
                                   event_id=event_id, text="Okay, dropping that suggestion.")
        return True
    if not is_yes(text, extra=payload.get("extra_yes") or ()):
        return False
    request_id = _open_prepared_request(
        conn, cfg, event_id=event_id,
        team_id=str(payload.get("team_id") or team_id),
        task=str(payload.get("task") or context.get("summary") or ""),
        fingerprint=str(payload.get("fingerprint") or context["context_ref"]))
    context_router.resolve_context(conn, context_id=context["id"], status="resolved")
    reply = str(payload.get("yes_reply") or "On it.")
    if request_id is None:
        reply = "Already working on that."
    context_router._emit_reply(conn, team_id=team_id, channel_ref=channel_ref,
                               event_id=event_id, text=reply)
    return True


def _handle_ask(conn, cfg, *, team_id, channel_ref, event_id, context, payload,
                text) -> bool:
    # ponytail: the next free-text message on the channel is taken as the
    # guidance (TTL-bound, dismissable, and the resulting request is
    # propose-only) - thread-anchored capture if misfires ever matter.
    from argus.v2.orchestrator import context_router
    if is_no(text):
        context_router.resolve_context(conn, context_id=context["id"], status="expired")
        context_router._emit_reply(conn, team_id=team_id, channel_ref=channel_ref,
                                   event_id=event_id, text="Okay, leaving it as is.")
        return True
    guidance = (text or "").strip()
    if not guidance:
        return False
    origin_request = str(payload.get("request_id") or "")
    task = (
        f"Owner guidance for a blocked task: {guidance}\n"
        f"Original task: {str(payload.get('task') or '')}\n"
        f"It was blocked on: {str(payload.get('blocker') or '')}\n"
        "Retry the task using the guidance. If it is still blocked, report the "
        "exact blocker."
    )
    request_id = _open_prepared_request(
        conn, cfg, event_id=event_id,
        team_id=str(payload.get("team_id") or team_id),
        task=task, fingerprint=f"unblock:{origin_request}")
    context_router.resolve_context(conn, context_id=context["id"], status="resolved")
    reply = "On it - retrying with your guidance."
    if request_id is None:
        reply = "Already retrying that one."
    context_router._emit_reply(conn, team_id=team_id, channel_ref=channel_ref,
                               event_id=event_id, text=reply)
    return True


def _open_prepared_request(conn, cfg, *, event_id, team_id, task,
                           fingerprint) -> str | None:
    """Rewrite the reply event's text to the prepared task and open a pipeline
    request on it (the event text is what the engine receives as the task)."""
    from argus.v2.orchestrator import pipeline
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE events SET payload=jsonb_set(payload, '{text}', %s::jsonb) "
            "WHERE id=%s", (Json(task), event_id))
    return pipeline.open_request(
        conn, cfg, event_id=event_id, team_id=team_id, conversation_id=None,
        fingerprint=fingerprint)
