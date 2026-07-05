"""Periodic reconciliation: every wake condition has a query here, so a missed
NOTIFY only adds latency. Idempotent; safe to run on a timer."""
from __future__ import annotations

import logging
import shutil
import time

import psycopg
from psycopg.types.json import Json

from argus.v2.actions import approvals, executor
from argus.v2.front import front
from argus.v2.ingress import events as ingress_events
from argus.v2.ingress.media import run_root
from argus.v2.orchestrator import budget, context_router, pipeline, status
from argus.v2.queue import jobs
from argus.v2.queue.models import ActionIntent, Job

log = logging.getLogger("argus.orchestrator")

# Throttle the "budget reached" log so a 2s poll loop does not spam it.
_BUDGET_LOG_EVERY = 300.0  # seconds
_last_budget_log = 0.0


def _log_budget_paused(cfg) -> None:
    global _last_budget_log
    nowm = time.monotonic()
    if nowm - _last_budget_log >= _BUDGET_LOG_EVERY:
        _last_budget_log = nowm
        log.warning("daily cost ceiling ($%.2f) reached; pausing new work until spend drops",
                    budget.ceiling(cfg))


def route_events(conn, cfg) -> int:
    if budget.over_budget(conn, cfg):
        _log_budget_paused(cfg)
        return 0  # pause opening new work; in-flight jobs still finish
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
        # Per-team budget: defer this team's event (back to 'received' with a
        # short backoff so the claim loop skips it instead of churning every
        # tick) when the team is over its own cap; other teams proceed.
        if budget.over_budget(conn, cfg, team_id):
            with conn.cursor() as cur:
                cur.execute("UPDATE events SET status='received', "
                            "defer_until=now() + interval '60 seconds' WHERE id=%s", (eid,))
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
                                          conversation_id=None, fingerprint=dedup_key,
                                          dedup_terminal=True)
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
            _emit_ack(conn, cfg, team_id, eid, conv_id)
        else:
            decision = front.decide(cfg, {"payload": payload})
            if decision.kind == "dispatch":
                pipeline.open_request(conn, cfg, event_id=eid, team_id=team_id,
                                      conversation_id=str(conv_id) if conv_id else None)
                _emit_ack(conn, cfg, team_id, eid, conv_id)
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


# A receipt posted the instant Argus claims a human message that triggers async
# work (manager converse, or a dispatch into the dev pipeline). Without it the
# chat goes silent for the seconds-to-minutes the engine/pipeline runs and the
# owner can't tell Argus even saw the message (owner hit this in the luma Slack
# group: said "hi", got nothing, didn't know if Argus was on it).
#
# On an edit-capable channel (Slack/Telegram/Discord) with progress enabled this
# becomes a single self-updating status line (see orchestrator/status.py) that
# advances receipt -> working -> reviewing -> done. Otherwise it is a one-shot
# reply. Idempotent per event; only posted when the conversation has a routable
# channel, so cli/no-channel events stay quiet. The executor's risk gate still
# applies, so this never auto-posts to an outward (customer-facing) channel.
_ACK_TEXT = status.RECEIPT


def _emit_ack(conn, cfg, team_id, event_id, conv_id) -> None:
    channel_ref = _channel_ref(conn, conv_id)
    if not _routable_channel(channel_ref):
        return
    ctype = channel_ref.split(":", 1)[0]
    from argus.v2.channels import send as _send
    if _progress_enabled(cfg, team_id) and _send.channel_supports_edit(ctype):
        status.post_initial(conn, team_id=team_id, event_id=event_id, channel_ref=channel_ref)
        return
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO actions (team_id, type, risk, destination_ref, "
            "  idempotency_key, payload) "
            "VALUES (%s,'reply','reversible_internal',%s,%s,%s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (team_id, channel_ref, f"ack:{event_id}", Json({"text": _ACK_TEXT})))


def _progress_enabled(cfg, team_id) -> bool:
    try:
        team = cfg.team(team_id)
    except KeyError:
        return False
    settings = team.notifications or cfg.company.defaults.notifications
    return bool(getattr(settings, "show_progress", True))


def _routable_channel(channel_ref) -> bool:
    """True only for a real outbound channel (slack/whatsapp/telegram/discord/
    email). A bare cli:local conversation has no waiting chat, so it gets no ack."""
    if not channel_ref or ":" not in channel_ref:
        return False
    from argus.v2.channels.base import REGISTRY
    return channel_ref.split(":", 1)[0] in REGISTRY


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
    _prune_orphan_worktrees(conn, wt_root)


def _prune_orphan_worktrees(conn: psycopg.Connection, wt_root) -> None:
    """Remove worktree dirs whose owning request/job row no longer exists at all.
    The terminal-request prune above can only match dirs for requests still in the
    table; when a request row is deleted (e.g. retention cleanup) its worktree dir
    is never selected and lingers forever (owner-hit: 5 orphan dirs from deleted
    requests). Here we GC any dir not backed by a live request id or job event_id,
    and only after a generous age window so a just-created worktree whose request
    row is momentarily absent is never reaped."""
    if not wt_root.exists():
        return
    with conn.cursor() as cur:
        cur.execute("SELECT id::text FROM requests")
        known = {row[0] for row in cur.fetchall()}
        cur.execute("SELECT DISTINCT event_id::text FROM jobs WHERE event_id IS NOT NULL")
        known |= {row[0] for row in cur.fetchall()}
    cutoff = time.time() - 30 * 60  # 30-minute grace
    for entry in wt_root.iterdir():
        if not entry.is_dir() or entry.name in known:
            continue
        try:
            if entry.stat().st_mtime > cutoff:
                continue  # too fresh to be sure it's an orphan
        except OSError:
            continue
        shutil.rmtree(entry, ignore_errors=True)


def _dead_job_error_detail(conn: psycopg.Connection, job_id: str, result) -> str:
    """Best-effort last error for a dead job. A job reclaimed straight to
    'dead' (lease timeout past max_attempts) never went through finalize(), so
    jobs.result is usually null; fall back to the most recent run row's output,
    same fields _last_failure_detail checks for a failed request."""
    if isinstance(result, dict):
        detail = str(result.get("test_output") or result.get("output")
                     or result.get("error") or "").strip()
        if detail:
            return detail[-1200:]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT output FROM runs WHERE job_id=%s ORDER BY started_at DESC LIMIT 1",
            (job_id,))
        row = cur.fetchone()
    detail = str(row[0] or "").strip() if row else ""
    return detail[-1200:] if detail else ""


def _notify_dead_jobs(conn: psycopg.Connection, cfg) -> int:
    """Surface every job that exhausted max_attempts (status='dead') to its
    team's control channel. Idempotent via 'dead-job:<job_id>' so a repeat
    sweep (or reclaim_expired flipping the same row again, which it cannot
    since dead is terminal) never double-alerts. Scoped to jobs created in the
    last 7 days and capped per sweep: on a database that predates this alert,
    an unbounded scan would flood every control channel with alerts for jobs
    that died long ago. Returns the number of alerts actually inserted."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, team_id, kind, attempts, max_attempts, result "
            "FROM jobs WHERE status='dead' "
            "  AND created_at > now() - interval '7 days' "
            "  AND NOT EXISTS (SELECT 1 FROM actions a "
            "    WHERE a.idempotency_key = 'dead-job:' || jobs.id::text) "
            "ORDER BY created_at DESC LIMIT 50"
        )
        rows = cur.fetchall()
    alerted = 0
    for job_id, team_id, kind, attempts, max_attempts, result in rows:
        job_id = str(job_id)
        detail = _dead_job_error_detail(conn, job_id, result)
        lines = [
            f"Job {job_id} (kind={kind}, team={team_id}) died after "
            f"{attempts}/{max_attempts} attempts.",
        ]
        if detail:
            lines += ["", "Last error:", detail]
        text = "\n".join(lines)
        dest = _team_control_dest(cfg, team_id) or "cli:local"
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO actions (job_id, team_id, type, risk, destination_ref, "
                "idempotency_key, payload) VALUES (%s,%s,'notify','reversible_internal',%s,%s,%s) "
                "ON CONFLICT (idempotency_key) DO NOTHING",
                (job_id, team_id, dest, f"dead-job:{job_id}", Json({"text": text})))
            if cur.rowcount:
                alerted += 1
    return alerted


def _source_team(cfg, source_name: str) -> str | None:
    """Look up the team a connector source routes to (company-scoped sources
    carry it directly; team-scoped sources belong to their team)."""
    for s in cfg.company.sources if cfg and cfg.company else []:
        if s.name == source_name:
            return s.team
    for t in cfg.teams if cfg else []:
        for s in t.sources:
            if s.name == source_name:
                return t.name
    return None


def _notify_connector_auth_failures(conn: psycopg.Connection, cfg) -> int:
    """Surface a connector stuck in an 'auth' failure (bad/expired key) to its
    team's control channel. A broken key otherwise backs off silently for up to
    an hour with nothing telling a human to go fix it. Idempotent via
    'connector-auth:<team>:<source>' so a still-broken key alerts once, not
    every sweep; a later success clears error_category (see driver._clear_failure)
    so a fixed-then-broken-again key can alert a second time."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_name, last_error FROM connector_state "
            "WHERE error_category='auth'")
        rows = cur.fetchall()
    alerted = 0
    for source_name, last_error in rows:
        team_id = _source_team(cfg, source_name)
        if not team_id:
            continue
        dest = _team_control_dest(cfg, team_id)
        if not dest:
            continue
        text = (f"Connector '{source_name}' is failing auth ({last_error or 'unauthorized'}). "
                f"The key is likely expired or revoked; polling is paused until it's fixed.")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO actions (team_id, type, risk, destination_ref, "
                "idempotency_key, payload) "
                "VALUES (%s,'notify','reversible_internal',%s,%s,%s) "
                "ON CONFLICT (idempotency_key) DO NOTHING",
                (team_id, dest, f"connector-auth:{team_id}:{source_name}", Json({"text": text})))
            if cur.rowcount:
                alerted += 1
    return alerted


def sweep_once(conn: psycopg.Connection, cfg) -> dict:
    """Run one reconciliation pass. Returns a counts dict of what happened
    (plus duration_ms) and logs a one-line summary: INFO when any work was
    done, DEBUG when the sweep was idle."""
    started = time.monotonic()
    counts: dict[str, int] = {}
    # 0. Route unprocessed events (front decision: reply vs dispatch).
    counts["events_routed"] = route_events(conn, cfg)
    # 1. Reclaim crashed jobs.
    counts["jobs_reclaimed"] = jobs.reclaim_expired(conn)
    # 1b. Alert on jobs that just went dead (exhausted max_attempts). Kind-
    # agnostic: on_job_done/_sweep_converse only react to dead status for
    # kinds they know how to advance, so without this a dead job outside those
    # paths (or one whose downstream handling swallows it) would never surface
    # to a human. Runs before the pipeline-advance step below so the alert
    # exists even if that step also fails the request.
    counts["dead_jobs_alerted"] = _notify_dead_jobs(conn, cfg)
    # 1c. Alert on connectors stuck failing auth (bad/expired key). Separate
    # from dead-job alerting: this is polled by launchd's own com.argus.poll
    # cadence, not a pipeline job, so it needs its own idempotent sweep here.
    counts["connector_auth_alerted"] = _notify_connector_auth_failures(conn, cfg)
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
    counts["pipeline_advanced"] = len(rows)
    # 2b. Handle done converse jobs not yet processed (no marker action yet).
    counts["async_jobs_handled"] = _sweep_converse(conn, cfg)
    # 2c. Close the loop on supabase bug sources: write Argus's verdict back to
    # the bug row so terminal requests stop looking ignored.
    counts["bug_writebacks"] = writeback_terminal_bugs(conn, cfg)
    # 3. Drain the action outbox.
    counts["actions_drained"] = executor.process_proposed(conn, cfg)
    # 4. Expire stale approvals.
    counts["approvals_expired"] = approvals.expire_due(conn)
    # 5. Prune worktree dirs for terminal requests.
    _prune_worktrees(conn, cfg)
    counts["duration_ms"] = int((time.monotonic() - started) * 1000)
    _log_sweep(counts)
    return counts


def _log_sweep(counts: dict) -> None:
    """One line per sweep: INFO with the nonzero counts when anything happened,
    DEBUG when idle so a healthy quiet loop does not fill the log."""
    busy = {k: v for k, v in counts.items() if v and k != "duration_ms"}
    if busy:
        summary = " ".join(f"{k}={v}" for k, v in busy.items())
        log.info("sweep did work: %s duration_ms=%d", summary,
                 counts["duration_ms"], extra={"sweep": counts})
    else:
        log.debug("sweep idle duration_ms=%d", counts["duration_ms"],
                  extra={"sweep": counts})


def writeback_terminal_bugs(conn: psycopg.Connection, cfg) -> int:
    """Propose bug_writeback action(s) for each terminal (done/failed) request
    that originated from a writeback-enabled supabase source and has no
    writeback action yet. A normal (non-batch) signal proposes one action,
    idempotency-keyed 'bug_writeback:<rid>' (unchanged). A batch signal
    (payload.kind == 'bug_batch', see the supabase connector's batch mode)
    proposes one action PER bug in the batch so every row gets its own verdict
    note, each idempotency-keyed 'bug_writeback:<rid>:<row_id>' so a partial
    prior write does not repeat and a missing one still gets added on resweep."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.team_id, r.status, e.source, e.payload
            FROM requests r JOIN events e ON e.id = r.event_id
            WHERE r.status IN ('done','failed') AND e.kind='signal'
              AND NOT EXISTS (
                SELECT 1 FROM actions a
                WHERE a.idempotency_key = 'bug_writeback:' || r.id::text
                   OR a.idempotency_key LIKE 'bug_writeback:' || r.id::text || ':%%')
            """
        )
        rows = cur.fetchall()
    proposed = 0
    for rid, team_id, status, source, payload in rows:
        src = _writeback_source(cfg, source)
        if src is None:
            continue
        id_col = (src.config or {}).get("id_column", "id")
        note = _bug_outcome_note(conn, str(rid), status)
        bug_rows = (payload or {}).get("rows") if (payload or {}).get("kind") == "bug_batch" else None
        if bug_rows:
            for finding in bug_rows:
                row_id = str((finding.get("row") or {}).get(id_col) or "")
                if not row_id:
                    continue
                proposed += _propose_bug_writeback(
                    conn, request_id=str(rid), team_id=team_id, source=source,
                    row_id=row_id, note=note,
                    idempotency_key=f"bug_writeback:{rid}:{row_id}")
        else:
            row_id = str(((payload or {}).get("row") or {}).get(id_col) or "")
            if not row_id:
                continue
            proposed += _propose_bug_writeback(
                conn, request_id=str(rid), team_id=team_id, source=source,
                row_id=row_id, note=note,
                idempotency_key=f"bug_writeback:{rid}")
    return proposed


def _propose_bug_writeback(conn: psycopg.Connection, *, request_id: str, team_id: str,
                           source: str, row_id: str, note: str,
                           idempotency_key: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO actions (request_id, team_id, type, risk, "
            " idempotency_key, payload) "
            "VALUES (%s,%s,'bug_writeback','reversible_internal',%s,%s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (request_id, team_id, idempotency_key,
             Json({"source_name": source, "row_id": row_id, "note": note})))
        return 1 if cur.rowcount else 0


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


def _sweep_converse(conn: psycopg.Connection, cfg) -> int:
    """Find terminal converse/triage/research jobs with no decision marker action
    (key '<kind>:<id>') and call pipeline.on_job_done for each. Idempotent: once
    the marker exists, the job is skipped on re-sweep. Returns jobs handled."""
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
    return len(ids)


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
