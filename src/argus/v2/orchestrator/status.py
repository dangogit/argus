"""Live progress status line: one self-updating message per inbound turn.

When Argus picks up a human message that triggers async work, it posts a single
``status`` action (idempotency key ``status:<event_id>``). Each lifecycle stage
rewrites that action's text and re-arms it, so the executor edits the SAME chat
message in place (Slack / Telegram / Discord) instead of posting a new message
per stage - the Claude-in-Slack feel:

    👀 Got it - looking into this...   ->   🛠️ working   ->   🔍 reviewing   ->   ✅ done

It is additive and best-effort: the substantive replies/notifies (the answer, the
PR-ready link) are unchanged, and a failed edit never blocks real work. Only
edit-capable conversation channels get a status line; everything else keeps the
one-shot receipt from the chat-receipt feature.
"""
from __future__ import annotations

from psycopg.types.json import Json

# Stage -> line. Kept short; the substantive content arrives as a separate reply.
RECEIPT = "👀 Got it - looking into this..."
WORKING = "🛠️ On it - working on a fix..."
REVIEWING = "🔍 Reviewing the change..."
DONE = "✅ Done."
NOFIX = "✅ Looked into it - no change needed."
FAILED = "⚠️ Couldn't finish - see details above."


def stage_line(role_name: str) -> str:
    """The status line for a pipeline stage role."""
    return REVIEWING if role_name in ("qa", "senior") else WORKING


# Native Slack assistant typing-dots (assistant.threads.setStatus): the grey
# italic indicator + rotating loading_messages shown alongside the durable emoji
# status line. Maps each progress line to (status, loading_messages); terminal
# lines clear the indicator (status=''). Any line not listed leaves it unchanged.
_TYPING = {
    RECEIPT: ("is looking into it...", ["Reading the message...", "Pulling up context..."]),
    WORKING: ("is working...", ["Making the change...", "Writing the code...", "Running checks..."]),
    REVIEWING: ("is reviewing...", ["Reviewing the diff...", "Double-checking..."]),
}
_TYPING_CLEAR = frozenset({DONE, NOFIX, FAILED})


def typing_for(text: str):
    """For a status line, return the native typing spec: ('', None) to CLEAR the
    indicator at a terminal line, (status, loading_messages) for a progress line,
    or None when the line maps to no typing change (leave whatever is showing)."""
    if text in _TYPING_CLEAR:
        return ("", None)
    return _TYPING.get(text)


def _event_thread_ts(conn, event_id):
    """The Slack thread_ts captured on the source event, if any. Other channels
    have no slack_thread_ts so this returns None and native typing is skipped."""
    if not event_id:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT payload->>'slack_thread_ts' FROM events WHERE id=%s", (event_id,))
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def post_initial(conn, *, team_id, event_id, channel_ref) -> None:
    """Post the first status line (the receipt) as a status action. Idempotent
    per event (key status:<event_id>), so a re-routed event posts only once. The
    source event's slack_thread_ts (when present) is stashed in the payload so the
    executor can fire native Slack typing-dots in the same thread; set_status only
    rewrites {text}, so it survives every later stage edit."""
    thread_ts = _event_thread_ts(conn, event_id)
    payload = {"text": RECEIPT}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO actions (team_id, type, risk, destination_ref, "
            "  idempotency_key, payload) "
            "VALUES (%s,'status','reversible_internal',%s,%s,%s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (team_id, channel_ref, f"status:{event_id}", Json(payload)))


def set_status(conn, event_id, text: str) -> None:
    """Rewrite the status line and re-arm it for an in-place edit. No-op when:
      - no status message exists for this event (progress disabled, non-editable
        channel, or signal-origin work) - the WHERE simply matches nothing;
      - the text is unchanged (guards against an infinite re-edit on idempotent
        sweeps, and dedupes same-line stages like qa->senior).
    Only re-arms from a settled state ('proposed'/'done'); an outward-channel
    status parked at awaiting_approval is left alone (never auto-released here)."""
    if not event_id:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE actions "
            "SET payload=jsonb_set(coalesce(payload,'{}'::jsonb),'{text}',to_jsonb(%s::text)), "
            "    status='proposed', updated_at=now() "
            "WHERE idempotency_key=%s AND type='status' "
            "  AND status IN ('proposed','done') "
            "  AND coalesce(payload->>'text','') <> %s",
            (text, f"status:{event_id}", text))


def set_status_for_request(conn, request_id, text: str) -> None:
    """set_status keyed by request_id (resolves the request's source event)."""
    with conn.cursor() as cur:
        cur.execute("SELECT event_id FROM requests WHERE id=%s", (request_id,))
        row = cur.fetchone()
    if row and row[0]:
        set_status(conn, str(row[0]), text)
