"""Respond-back to signal origins: every reporter hears the outcome.

A signal may carry a 'reply_to' origin descriptor in its payload, e.g.
{"kind": "supabase_bug_reports", "row_id": ...} or
{"kind": "slack_thread", "channel": ..., "ts": ...}. When the work a signal
produced reaches a terminal state (request done/failed, or the PM triaged it
as ignore / research said no_fix), the sweep proposes a short reply to that
origin through the actions outbox, so the normal risk/approval gates apply
(a reply to an outward channel still pauses for approval; see
executor._effective_risk).

Opt-in per source via config 'respond: true' (default off). The supabase
'writeback: true' opt-in keeps working unchanged and implies the
supabase_bug_reports responder, including for signals ingested before this
feature existed (their payload has a 'row' but no 'reply_to').

New origin kinds register with @responder("<kind>"); a responder receives a
ReplyContext and proposes idempotency-keyed action rows, returning how many
it inserted.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

import psycopg
from psycopg.types.json import Json

log = logging.getLogger("argus.orchestrator")

SUPABASE_KIND = "supabase_bug_reports"

RESPONDERS: dict[str, Callable] = {}


def responder(kind: str):
    def _register(fn):
        RESPONDERS[kind] = fn
        return fn
    return _register


@dataclass
class ReplyContext:
    """Everything a responder needs to address one origin. 'anchor' is the id
    the idempotency key hangs off: the request id, or the event id for
    ignored signals that never opened a request."""
    anchor: str
    request_id: Optional[str]
    event_id: str
    team_id: str
    source: str      # source name in config
    src: object      # the SourceRef (config dict carries column names etc.)
    ref: dict        # the reply_to descriptor ({} for legacy supabase signals)
    payload: dict    # full event payload
    note: str        # short outcome text to post


# A reply already exists for this anchor id (either key namespace: the
# supabase responder keeps the pre-existing 'bug_writeback:' keys so live
# installs do not re-write rows that were already written back).
_NOT_REPLIED = (
    "NOT EXISTS (SELECT 1 FROM actions a "
    " WHERE a.idempotency_key = 'bug_writeback:' || {id}::text "
    "    OR a.idempotency_key LIKE 'bug_writeback:' || {id}::text || ':%%' "
    "    OR a.idempotency_key = 'respond:' || {id}::text "
    "    OR a.idempotency_key LIKE 'respond:' || {id}::text || ':%%')"
)


def sweep(conn: psycopg.Connection, cfg) -> int:
    """Propose origin replies for all newly-terminal signal work. Idempotent;
    called from reconcile.sweep_once. Returns actions proposed."""
    return respond_terminal_requests(conn, cfg) + respond_ignored_signals(conn, cfg)


def respond_terminal_requests(conn: psycopg.Connection, cfg) -> int:
    """One reply per terminal (done/failed) request that originated from a
    signal with a resolvable, respond-enabled origin."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.id, r.team_id, r.status, e.id, e.source, e.payload "
            "FROM requests r JOIN events e ON e.id = r.event_id "
            "WHERE r.status IN ('done','failed') AND e.kind='signal' AND "
            + _NOT_REPLIED.format(id="r.id"))
        rows = cur.fetchall()
    proposed = 0
    for rid, team_id, status, eid, source, payload in rows:
        route = _route(cfg, source, payload or {})
        if route is None:
            continue
        kind, src, ref = route
        ctx = ReplyContext(anchor=str(rid), request_id=str(rid), event_id=str(eid),
                           team_id=team_id, source=source, src=src, ref=ref,
                           payload=payload or {},
                           note=outcome_note(conn, str(rid), status))
        proposed += RESPONDERS[kind](conn, ctx)
    return proposed


def respond_ignored_signals(conn: psycopg.Connection, cfg) -> int:
    """A signal the PM triaged as ignore (or research concluded no_fix) never
    opens a request, so the terminal-request path cannot see it. The triage /
    research marker action (payload.triage, see pipeline._triage_marker) is the
    terminal record; reply once per event, keyed on the event id."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT e.id, j.team_id, e.source, e.payload, "
            "  a.payload->>'triage', a.payload->>'text' "
            "FROM jobs j "
            "JOIN actions a ON a.idempotency_key = j.kind || ':' || j.id::text "
            "JOIN events e ON e.id = j.event_id "
            "WHERE j.kind IN ('triage','research') "
            "  AND j.status = 'done' "
            "  AND a.payload->>'triage' IN ('ignore','no_fix') "
            "  AND e.kind='signal' AND " + _NOT_REPLIED.format(id="e.id"))
        rows = cur.fetchall()
    proposed = 0
    for eid, team_id, source, payload, decision, text in rows:
        route = _route(cfg, source, payload or {})
        if route is None:
            continue
        kind, src, ref = route
        ctx = ReplyContext(anchor=str(eid), request_id=None, event_id=str(eid),
                           team_id=team_id, source=source, src=src, ref=ref,
                           payload=payload or {},
                           note=_ignored_note(decision, text))
        proposed += RESPONDERS[kind](conn, ctx)
    return proposed


def _route(cfg, source_name: str, payload: dict):
    """Resolve (kind, source, reply_to) for a signal, or None when the source
    is unknown, the kind has no responder, or respond-back is not enabled."""
    src = _source_by_name(cfg, source_name)
    if src is None:
        return None
    ref = payload.get("reply_to")
    kind = str(ref.get("kind") or "") if isinstance(ref, dict) else ""
    if not kind and getattr(src, "type", "") == "supabase":
        # Legacy: signals ingested before reply_to existed still carry the bug
        # row in the payload; the supabase responder reads it from there.
        kind = SUPABASE_KIND
    if kind not in RESPONDERS or not _enabled(src, kind):
        return None
    return kind, src, (ref if isinstance(ref, dict) else {})


def _enabled(src, kind: str) -> bool:
    cfgd = src.config or {}
    if cfgd.get("respond"):
        return True
    # writeback: true predates respond: true and stays the supabase opt-in.
    return kind == SUPABASE_KIND and bool(cfgd.get("writeback"))


def _source_by_name(cfg, name: str):
    for s in (cfg.company.sources if cfg and cfg.company else []):
        if s.name == name:
            return s
    for t in (cfg.teams if cfg else []):
        for s in t.sources:
            if s.name == name:
                return s
    return None


def outcome_note(conn: psycopg.Connection, request_id: str, status: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT provider_ref FROM actions WHERE request_id=%s AND type='open_pr' "
            "AND provider_ref LIKE 'http%%' ORDER BY created_at DESC LIMIT 1",
            (request_id,))
        row = cur.fetchone()
    if row and row[0]:
        return f"Argus investigated this and opened a PR: {row[0]}"
    if status == "failed":
        return ("Argus attempted an automated fix but it did not pass review. "
                "Needs a human.")
    return "Argus investigated; no automated code fix was warranted."


def _ignored_note(decision: str, text: Optional[str]) -> str:
    reason = " ".join(str(text or "").split())
    if len(reason) > 300:
        reason = reason[:297].rstrip() + "..."
    if decision == "no_fix":
        base = "Argus investigated; no automated code fix was warranted."
    else:
        base = "Argus triaged this signal and decided not to act (noise / expected / not actionable)."
    return f"{base} {reason}".strip()


# --- responders -------------------------------------------------------------


@responder(SUPABASE_KIND)
def _respond_supabase(conn: psycopg.Connection, ctx: ReplyContext) -> int:
    """Write the verdict back to the bug row(s) via the existing bug_writeback
    action (actions/handlers._run_bug_writeback). A batch signal (payload.kind
    'bug_batch') fans out one action per bug row, each with its own key so a
    partial prior write does not repeat and a missing one is added on resweep."""
    id_col = (ctx.src.config or {}).get("id_column", "id")
    bug_rows = ctx.payload.get("rows") if ctx.payload.get("kind") == "bug_batch" else None
    proposed = 0
    if bug_rows:
        for finding in bug_rows:
            row_id = str((finding.get("row") or {}).get(id_col) or "")
            if not row_id:
                continue
            proposed += _propose_bug_writeback(
                conn, ctx, row_id=row_id,
                idempotency_key=f"bug_writeback:{ctx.anchor}:{row_id}")
    else:
        row_id = str((ctx.payload.get("row") or {}).get(id_col)
                     or ctx.ref.get("row_id") or "")
        if row_id:
            proposed += _propose_bug_writeback(
                conn, ctx, row_id=row_id,
                idempotency_key=f"bug_writeback:{ctx.anchor}")
    return proposed


def _propose_bug_writeback(conn: psycopg.Connection, ctx: ReplyContext, *,
                           row_id: str, idempotency_key: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO actions (request_id, team_id, type, risk, "
            " idempotency_key, payload) "
            "VALUES (%s,%s,'bug_writeback','reversible_internal',%s,%s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (ctx.request_id, ctx.team_id, idempotency_key,
             Json({"source_name": ctx.source, "row_id": row_id, "note": ctx.note})))
        return 1 if cur.rowcount else 0


@responder("slack_thread")
def _respond_slack_thread(conn: psycopg.Connection, ctx: ReplyContext) -> int:
    """Reply in the originating Slack thread through the normal channel outbox
    (destination 'slack:<channel>' resolves via the team's channel binding;
    thread_ts threads the reply under the original message)."""
    channel = str(ctx.ref.get("channel") or "")
    if not channel:
        return 0
    payload: dict = {"text": ctx.note}
    ts = ctx.ref.get("thread_ts") or ctx.ref.get("ts")
    if ts:
        payload["thread_ts"] = str(ts)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO actions (request_id, team_id, type, risk, "
            " destination_ref, idempotency_key, payload) "
            "VALUES (%s,%s,'reply','reversible_internal',%s,%s,%s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (ctx.request_id, ctx.team_id, f"slack:{channel}",
             f"respond:{ctx.anchor}", Json(payload)))
        return 1 if cur.rowcount else 0
