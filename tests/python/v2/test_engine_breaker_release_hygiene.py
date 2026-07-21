"""Release hygiene for the engine breaker (2026-07-21): a codex usage-limit
outage keeps the breaker open for days, and work-chat claims/releases the same
job every escalation window with log line "released job ... (reason=engine
outage: codex delay=...s releases=...)". Three hard guarantees this must never
turn into wasted churn or Slack spam:

1. A job released for a breaker-open outage gets run_after >= the breaker's
   own retry-at T (jobs table), not some shorter fixed delay -- so the queue
   never claims/release-spins faster than the breaker's own cooldown.
2. Outage release never burns the job's attempt budget (regression guard;
   believed true already since PR #47 -- kept green to lock it in).
3. An outage release creates zero notify action rows, and the breaker-open
   alert fires exactly once per episode (not once per escalation) -- else a
   multi-day outage pages roughly hourly once the breaker's own 1h cooldown
   cap lines up with the alert's 1h dedup cooldown.
"""
from __future__ import annotations

from argus.v2.ingress import events
from argus.v2.orchestrator import pipeline
from argus.v2.queue.models import RunRecord
from argus.v2.workspace.repo import Worktree
from argus.v2.worker import worker


def _open_request(conn, cfg_project, key="k1", text="fix it"):
    eid = events.ingest_message(conn, cfg_project, team="dev", source="cli",
                                dedup_key=key, text=text)
    conn.commit()
    rid = pipeline.open_request(conn, cfg_project, event_id=eid, team_id="dev",
                                conversation_id=None)
    conn.commit()
    return eid, rid


def _snap_engine(conn, rid, engine="codex"):
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE jobs SET exec_snapshot =
                   jsonb_set(COALESCE(exec_snapshot,'{}'::jsonb), '{engine}', to_jsonb(%s::text))
               WHERE request_id=%s""",
            (engine, rid))
    conn.commit()


def _outage_run_job(cfg, job, context, workdir):
    run = RunRecord(role=job.role, engine="codex", status="outage",
                    prompt="p", output="You've hit your usage limit.")
    return run, {"error": "You've hit your usage limit."}, []


def _stub_worktree(monkeypatch):
    monkeypatch.setattr(worker.workspace, "create_worktree",
                        lambda project, request_id: Worktree("/tmp", "b", "/tmp"))
    monkeypatch.setattr(worker.workspace, "commit_all", lambda path, message: False)
    monkeypatch.setattr(worker.workspace, "diff", lambda project, path: "")


def test_release_honors_breaker_window_not_shorter_fixed_delay(conn, cfg_project,
                                                                 monkeypatch):
    """Breaker open with retry-at T in the future: the released job's
    run_after must be >= T, not a shorter fixed delay, or the queue would
    claim/release-spin below the breaker's own cooldown window."""
    from argus.v2.queue import breaker

    _, rid = _open_request(conn, cfg_project, key="breaker-window-1")
    _snap_engine(conn, rid)
    breaker.trip(conn, "codex", "usage limit")
    # Force a deliberately large, distinctive retry-at so a shorter fixed
    # delay bug (e.g. a flat few-minute sleep) would clearly fail this.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE engine_breaker SET open_until = now() + interval '5000 seconds'"
            " WHERE engine='codex'")
        cur.execute("SELECT open_until FROM engine_breaker WHERE engine='codex'")
        t_retry_at = cur.fetchone()[0]
    conn.commit()

    def explode(*a, **kw):
        raise AssertionError("engine must not be called while breaker is open")
    monkeypatch.setattr(worker.job_exec, "run_job", explode)
    monkeypatch.setattr(worker.workspace, "create_worktree", explode)

    assert worker.run_once(cfg_project, "w1") is True
    with conn.cursor() as cur:
        cur.execute("SELECT run_after FROM jobs WHERE request_id=%s", (rid,))
        run_after = cur.fetchone()[0]
    assert run_after >= t_retry_at


def test_outage_release_does_not_burn_attempt(conn, cfg_project, monkeypatch):
    """Regression guard (believed true already, PR #47): releasing for engine
    outage must not increment jobs.attempts -- the failure belongs to the
    engine, not the job."""
    _, rid = _open_request(conn, cfg_project, key="outage-attempt-1")
    _snap_engine(conn, rid)
    _stub_worktree(monkeypatch)
    monkeypatch.setattr(worker.job_exec, "run_job", _outage_run_job)

    assert worker.run_once(cfg_project, "w1") is True
    with conn.cursor() as cur:
        cur.execute("SELECT status, attempts FROM jobs WHERE request_id=%s", (rid,))
        status, attempts = cur.fetchone()
    assert status == "pending"
    assert attempts == 0


def test_outage_release_creates_no_notify_actions(conn, cfg_project, monkeypatch):
    """An outage release is queue bookkeeping, not owner-facing communication:
    it must not insert any notify (or other) action rows."""
    _, rid = _open_request(conn, cfg_project, key="outage-notify-1")
    _snap_engine(conn, rid)
    _stub_worktree(monkeypatch)
    monkeypatch.setattr(worker.job_exec, "run_job", _outage_run_job)

    assert worker.run_once(cfg_project, "w1") is True
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM actions WHERE request_id=%s", (rid,))
        assert cur.fetchone()[0] == 0


def test_breaker_alert_fires_once_per_episode_not_per_escalation(conn, cfg_project,
                                                                  monkeypatch):
    """A multi-day outage re-escalates the breaker (and re-releases the job)
    every time its own cooldown window expires and a probe attempt fails
    again -- the breaker's cooldown caps at 1h (MAX_COOLDOWN_SECONDS), which
    is the SAME window as the alert's own dedup cooldown. Once both line up,
    a naive "alert on every trip" fires roughly hourly for days. The
    breaker-open alert must fire exactly once for the whole episode (between
    breaker-open and the next breaker.reset), not once per escalation."""
    _, rid = _open_request(conn, cfg_project, key="breaker-episode-1")
    _snap_engine(conn, rid)
    _stub_worktree(monkeypatch)
    monkeypatch.setattr(worker.job_exec, "run_job", _outage_run_job)

    # First failure opens the episode: exactly one alert.
    assert worker.run_once(cfg_project, "w1") is True
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM alerts WHERE fingerprint='engine-outage-codex'")
        assert cur.fetchone()[0] == 1

    # Simulate both the breaker's own cooldown AND the alert's dedup cooldown
    # having fully elapsed (the real-world case once they line up), with the
    # engine STILL down -- same episode, since reset() never ran.
    with conn.cursor() as cur:
        cur.execute("UPDATE engine_breaker SET open_until = now() - interval '1 second'"
                    " WHERE engine='codex'")
        cur.execute("UPDATE jobs SET run_after = now() WHERE request_id=%s", (rid,))
        cur.execute("UPDATE alerts SET ts = ts - interval '2 hours'"
                    " WHERE fingerprint='engine-outage-codex'")
    conn.commit()

    assert worker.run_once(cfg_project, "w1") is True
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM alerts WHERE fingerprint='engine-outage-codex'")
        # Still the same unresolved episode -- exactly one alert total, not two.
        assert cur.fetchone()[0] == 1
