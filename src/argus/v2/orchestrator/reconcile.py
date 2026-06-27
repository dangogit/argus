"""Periodic reconciliation: every wake condition has a query here, so a missed
NOTIFY only adds latency. Idempotent; safe to run on a timer."""
from __future__ import annotations

import shutil

import psycopg
from psycopg.types.json import Json

from argus.v2.actions import approvals, executor
from argus.v2.front import front
from argus.v2.ingress import events as ingress_events
from argus.v2.ingress.media import run_root
from argus.v2.orchestrator import context_router, pipeline
from argus.v2.queue import jobs
from argus.v2.queue.models import ActionIntent, Job


def route_events(conn, cfg) -> int:
    rows = ingress_events.claim_unprocessed(conn)
    handled = 0
    for (eid, team_id, kind, conv_id, dedup_key, payload) in rows:
        eid = str(eid)
        if not _has_team(cfg, team_id):
            with conn.cursor() as cur:
                cur.execute("UPDATE events SET status='processed', processed_at=now() WHERE id=%s",
                            (eid,))
            handled += 1
            continue
        channel_ref = _channel_ref(conn, conv_id)
        if kind == "signal":
            if _is_drift(payload) and _notify_drift(conn, cfg, team_id, payload, dedup_key):
                # Branch drift is not a code fix (Argus won't auto-merge branches);
                # surface it to the owner's control channel instead of dispatching.
                pass
            elif pipeline.is_actionable(payload):
                if _conversational(cfg, team_id):
                    # Route the signal through the team PM to triage (investigate
                    # via researcher / dispatch a fix / ignore) instead of blindly
                    # opening a code-fix pipeline.
                    pipeline.enqueue_triage(conn, cfg, event_id=eid, team_id=team_id,
                                            fingerprint=dedup_key)
                else:
                    pipeline.open_request(conn, cfg, event_id=eid, team_id=team_id,
                                          conversation_id=None, fingerprint=dedup_key)
            # else: internal-noise/empty signal -> consume without opening a
            # request (event is marked processed below).
        elif channel_ref and context_router.handle_message(
                conn, cfg, team_id=team_id, channel_ref=channel_ref,
                event_id=eid, text=(payload or {}).get("text", "")):
            pass
        elif _handle_support_guidance(conn, cfg, team_id, payload):
            pass
        elif _conversational(cfg, team_id):
            # Manager role has a configured engine: async converse job.
            pipeline.enqueue_converse(conn, cfg, event_id=eid, team_id=team_id,
                                      conversation_id=str(conv_id) if conv_id else None)
        else:
            decision = front.decide(cfg, {"payload": payload})
            if decision.kind == "dispatch":
                pipeline.open_request(conn, cfg, event_id=eid, team_id=team_id,
                                      conversation_id=str(conv_id) if conv_id else None)
            else:
                _emit_reply(conn, team_id, eid, conv_id, decision.reply_text)
        with conn.cursor() as cur:
            cur.execute("UPDATE events SET status='processed', processed_at=now() WHERE id=%s",
                        (eid,))
        handled += 1
    return handled


def _has_team(cfg, team_id: str | None) -> bool:
    if not team_id:
        return False
    try:
        cfg.team(team_id)
        return True
    except KeyError:
        return False


def _handle_support_guidance(conn, cfg, team_id: str, payload: dict) -> bool:
    text = (payload or {}).get("text", "")
    if "support " not in text.lower():
        return False
    try:
        from argus.v2.support import cycle as support_cycle
        return support_cycle.handle_guidance_reply(conn, cfg, team_id, text)
    except KeyError:
        return False


def _is_drift(payload) -> bool:
    p = payload or {}
    return p.get("kind") == "branch_drift" or p.get("source") == "branch_drift"


def _team_control_dest(cfg, team_id: str) -> str | None:
    try:
        team = cfg.team(team_id)
    except KeyError:
        return None
    for ch in getattr(team, "channels", []) or []:
        if ch.role == "control" and ch.type != "cli":
            return f"{ch.type}:{ch.channel_id}"
    return None


def _notify_drift(conn, cfg, team_id: str, payload, dedup_key) -> bool:
    """Surface a branch-drift signal to the team's control channel (the owner
    decides whether to sync), instead of dispatching a dev pipeline. Idempotent
    per drift fingerprint. Returns False if the team has no control channel, so
    the caller falls through to the normal path."""
    dest = _team_control_dest(cfg, team_id)
    if not dest:
        return False
    msg = (payload or {}).get("message") or "branch drift detected"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO actions (team_id, type, risk, destination_ref, "
            "  idempotency_key, payload) "
            "VALUES (%s,'notify','reversible_internal',%s,%s,%s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (team_id, dest, f"drift-notify:{dedup_key}",
             Json({"text": f"⚠️ {msg}. Sync the branches when ready "
                           f"(Argus does not auto-merge)."})))
    context_router.register_context(
        conn, team_id=team_id, channel_ref=dest, context_type="branch_drift",
        context_ref=str(dedup_key), summary=msg, payload=dict(payload or {}),
        ttl_hours=72)
    return True


def _conversational(cfg, team_id: str) -> bool:
    """True if the team has a manager role with a resolvable engine (the
    conversational front is enabled for this team)."""
    from argus.v2.config import loader
    try:
        team = cfg.team(team_id)
        role = team.role("manager")
    except KeyError:
        return False
    if role.kind not in ("front",):
        return False
    eng = loader.resolve_engine(cfg, team_id, "manager")
    # Only enable the conversational path when the role carries an explicit
    # engine (not just the company/team default fallback to echo).
    return role.engine is not None and eng.engine != "echo"


def _channel_ref(conn, conv_id) -> str | None:
    if not conv_id:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT channel_ref FROM conversations WHERE id=%s", (conv_id,))
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def _emit_reply(conn, team_id, event_id, conv_id, text) -> None:
    dest = f"conv:{conv_id}"
    with conn.cursor() as cur:
        if conv_id:
            cur.execute("SELECT channel_ref FROM conversations WHERE id=%s", (conv_id,))
            r = cur.fetchone()
            if r and r[0]:
                dest = r[0]
        cur.execute(
            """INSERT INTO actions (team_id, type, risk, destination_ref,
                                    idempotency_key, payload)
               VALUES (%s,'reply','reversible_internal',%s,%s,%s)
               ON CONFLICT (idempotency_key) DO NOTHING""",
            (team_id, dest, f"reply:{event_id}", Json({"text": text})))


def _load_job(conn: psycopg.Connection, job_id: str) -> Job:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, request_id, event_id, conversation_id, team_id, role, "
            "stage, kind, status, attempts, max_attempts, claim_token, "
            "exec_snapshot, payload FROM jobs WHERE id=%s", (job_id,))
        r = cur.fetchone()
    return Job(id=str(r[0]), request_id=str(r[1]) if r[1] else None,
               event_id=str(r[2]) if r[2] else None,
               conversation_id=str(r[3]) if r[3] else None, team_id=r[4],
               role=r[5], stage=r[6], kind=r[7], status=r[8], attempts=r[9],
               max_attempts=r[10], claim_token=str(r[11]) if r[11] else None,
               exec_snapshot=r[12], payload=r[13])


def _prune_worktrees(conn: psycopg.Connection, cfg) -> None:
    """Remove leftover worktree dirs for terminal requests, but only once they
    have aged past a grace window AND have no non-terminal action still
    depending on the worktree. Two hazards this guards against:
      - a just-reclaimed (dead) job whose zombie engine process may still be
        writing into the worktree (the grace window lets it settle);
      - an approval-gated open_pr action that is parked awaiting approval while
        the request is already 'done' (it still needs the worktree to push).
    Only acts when the dir exists (cheap, idempotent). Safe to call repeatedly."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.id FROM requests r "
            "WHERE r.status IN ('done','failed','cancelled') "
            "  AND r.updated_at < now() - interval '2 minutes' "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM actions a WHERE a.request_id = r.id "
            "      AND a.status IN ('proposed','approved','executing','awaiting_approval'))"
        )
        rids = [str(row[0]) for row in cur.fetchall()]
        # Research jobs get a read-only worktree keyed by their event_id; they
        # commit nothing, so prune once terminal.
        cur.execute(
            "SELECT event_id FROM jobs WHERE kind='research' "
            "  AND status IN ('done','failed','dead') AND event_id IS NOT NULL")
        rids += [str(row[0]) for row in cur.fetchall()]
    wt_root = run_root() / "worktrees"
    for rid in rids:
        wt_dir = wt_root / rid
        if wt_dir.exists():
            shutil.rmtree(wt_dir, ignore_errors=True)


def sweep_once(conn: psycopg.Connection, cfg) -> None:
    # 0. Route unprocessed events (front decision: reply vs dispatch).
    route_events(conn, cfg)
    # 1. Reclaim crashed jobs.
    jobs.reclaim_expired(conn)
    # 2. Advance pipelines for terminal jobs not yet advanced.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT j.id, j.team_id FROM jobs j JOIN requests r ON r.id=j.request_id
            WHERE j.kind='pipeline' AND j.status IN ('done','failed','dead')
              AND r.status='open'
              AND j.advanced_at IS NULL
            ORDER BY j.updated_at
            """,
        )
        rows = [(str(row[0]), row[1]) for row in cur.fetchall()]
    for jid, team_id in rows:
        if not _has_team(cfg, team_id):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE requests SET status='failed', updated_at=now() "
                    "WHERE id=(SELECT request_id FROM jobs WHERE id=%s) AND status='open'",
                    (jid,))
                cur.execute("UPDATE jobs SET advanced_at=now(), updated_at=now() WHERE id=%s",
                            (jid,))
            continue
        pipeline.on_job_done(conn, cfg, _load_job(conn, jid))
        with conn.cursor() as cur:
            cur.execute("UPDATE jobs SET advanced_at=now() WHERE id=%s", (jid,))
    # 2b. Handle done converse jobs not yet processed (no marker action yet).
    _sweep_converse(conn, cfg)
    # 2c. Close the loop on supabase bug sources: write Argus's verdict back to
    # the bug row so terminal requests stop looking ignored.
    writeback_terminal_bugs(conn, cfg)
    # 3. Drain the action outbox.
    executor.process_proposed(conn, cfg)
    # 4. Expire stale approvals.
    approvals.expire_due(conn)
    # 5. Prune worktree dirs for terminal requests.
    _prune_worktrees(conn, cfg)


def writeback_terminal_bugs(conn: psycopg.Connection, cfg) -> int:
    """Propose a bug_writeback action for each terminal (done/failed) request
    that originated from a writeback-enabled supabase source and has no
    writeback action yet. Idempotent via idempotency_key bug_writeback:<rid>."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.team_id, r.status, e.source, e.payload
            FROM requests r JOIN events e ON e.id = r.event_id
            WHERE r.status IN ('done','failed') AND e.kind='signal'
              AND NOT EXISTS (
                SELECT 1 FROM actions a
                WHERE a.idempotency_key = 'bug_writeback:' || r.id::text)
            """
        )
        rows = cur.fetchall()
    proposed = 0
    for rid, team_id, status, source, payload in rows:
        src = _writeback_source(cfg, source)
        if src is None:
            continue
        id_col = (src.config or {}).get("id_column", "id")
        row_id = str(((payload or {}).get("row") or {}).get(id_col) or "")
        if not row_id:
            continue
        note = _bug_outcome_note(conn, str(rid), status)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO actions (request_id, team_id, type, risk, "
                " idempotency_key, payload) "
                "VALUES (%s,%s,'bug_writeback','reversible_internal',%s,%s) "
                "ON CONFLICT (idempotency_key) DO NOTHING",
                (str(rid), team_id, f"bug_writeback:{rid}",
                 Json({"source_name": source, "row_id": row_id, "note": note})))
            if cur.rowcount:
                proposed += 1
    return proposed


def _writeback_source(cfg, source_name: str):
    for s in (cfg.company.sources if cfg and cfg.company else []):
        if (s.name == source_name and s.type == "supabase"
                and (s.config or {}).get("writeback")):
            return s
    return None


def _bug_outcome_note(conn: psycopg.Connection, request_id: str, status: str) -> str:
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


def _sweep_converse(conn: psycopg.Connection, cfg) -> None:
    """Find terminal converse/triage/research jobs with no decision marker action
    (key '<kind>:<id>') and call pipeline.on_job_done for each. Idempotent: once
    the marker exists, the job is skipped on re-sweep."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT j.id FROM jobs j
            WHERE j.kind IN ('converse','triage','research')
              AND j.status IN ('done','failed','dead')
              AND NOT EXISTS (
                SELECT 1 FROM actions a
                WHERE a.idempotency_key = j.kind || ':' || j.id::text)
            """
        )
        ids = [str(row[0]) for row in cur.fetchall()]
    for jid in ids:
        job = _load_job(conn, jid)
        if not _has_team(cfg, job.team_id):
            _mark_async_job_skipped(conn, job, "missing_team")
            continue
        pipeline.on_job_done(conn, cfg, job)


def _mark_async_job_skipped(conn: psycopg.Connection, job: Job, reason: str) -> None:
    """Record a terminal marker for stale async jobs whose team is gone.
    Config churn should not keep crashing `argus up` forever."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO actions (job_id, team_id, type, risk, idempotency_key, "
            "status, payload) "
            "VALUES (%s,%s,'notify','reversible_internal',%s,'done',%s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (job.id, job.team_id, f"{job.kind}:{job.id}", Json({"skipped": reason})),
        )
