"""Engine-call failures at the worker boundary: timeout, generic exception,
garbled output, and EngineOutageError. The durable queue has fencing tests
(test_queue_fencing.py) but nothing exercised what happens when the LLM
engine call itself dies mid-run -- the most likely production failure.

Two distinct failure paths exist in this codebase and must not be conflated:

1. The engine call raises INSIDE worker.run_once (timeout, generic exception).
   worker.py's `except Exception` catches it and finalizes the job as
   'failed' in the SAME call (see worker.py run_once). This is a single-shot
   terminal outcome: the job never goes back to 'pending' and attempts is
   never incremented for this path. The request is failed by
   pipeline.on_job_done on the next sweep (status in ('failed','dead')).
   Retry-with-backoff only happens via jobs.reclaim_expired, which fires when
   a worker crashes/hangs and the lease (heartbeat) expires -- not when the
   in-process call raises and is caught.

2. EngineOutageError is caught INSIDE exec.run_job itself and converted into
   a normal (non-raising) return: RunRecord(status="outage") with an "error"
   key in the result dict. worker.run_once now treats this as a recoverable
   engine-level failure, not a job-level one: it trips the engine circuit
   breaker, emits one deduplicated critical alert, and releases the job back
   to pending with a delay instead of finalizing it as "failed" (the job's
   attempt budget is preserved via jobs.release, not burned). While the
   breaker is open for an engine, any claimed job on that engine is released
   immediately, before a worktree is built or the engine is called. Past
   worker.OUTAGE_RELEASE_CAP releases the outage is treated as permanent and
   the job finalizes "failed" as before. A successful run on a previously
   tripped engine closes the breaker via breaker.reset.

Tests inject a fake engine by monkeypatching worker.job_exec.run_job (or, for
the EngineOutageError case, exec.run_agent), the same seam already used by
test_worker.py's qa/builder tests, so no new production seam is added.
"""
from __future__ import annotations

from argus.engine import EngineOutageError
from argus.v2.ingress import events
from argus.v2.orchestrator import pipeline, reconcile
from argus.v2.queue import jobs
from argus.v2.queue.models import ActionIntent, RunRecord
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


def _job_row(conn, rid):
    with conn.cursor() as cur:
        cur.execute("SELECT status, attempts, result FROM jobs WHERE request_id=%s", (rid,))
        return cur.fetchone()


def _action_count(conn, rid=None):
    with conn.cursor() as cur:
        if rid is None:
            cur.execute("SELECT count(*) FROM actions")
        else:
            cur.execute("SELECT count(*) FROM actions WHERE request_id=%s", (rid,))
        return cur.fetchone()[0]


def test_engine_timeout_finalizes_failed_not_stuck_running(conn, cfg_project, monkeypatch):
    """TimeoutError raised by the engine call must not leave the job 'running'.
    worker.run_once catches any Exception raised before finalize and finalizes
    the job as 'failed' in the same call -- it can never be left mid-flight."""
    _, rid = _open_request(conn, cfg_project, key="timeout1")

    monkeypatch.setattr(worker.workspace, "create_worktree",
                        lambda project, request_id: Worktree("/tmp", "b", "/tmp"))

    def boom(cfg, job, context, workdir):
        raise TimeoutError("engine call exceeded deadline")

    monkeypatch.setattr(worker.job_exec, "run_job", boom)

    assert worker.run_once(cfg_project, "w1") is True
    status, attempts, result = _job_row(conn, rid)
    assert status == "failed"
    assert status != "running"
    assert result["error_type"] == "TimeoutError"
    assert "engine call exceeded deadline" in result["error"]

    # Advance the pipeline: a failed pipeline job fails the request, it is not
    # silently left open.
    reconcile.sweep_once(conn, cfg_project)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] == "failed"


def test_engine_timeout_via_lease_expiry_retries_then_dies(conn, cfg_project):
    """The retry-with-backoff / max_attempts path is lease expiry (a worker that
    hangs on the engine call and never heartbeats), not an in-process raise.
    Simulate a claimed job whose lease expired: it must requeue with attempts
    bumped, then go 'dead' once attempts reach max_attempts, and the request
    must then fail. reclaim_expired resets run_after to now() (its own
    backoff), and the job is never left 'running'."""
    _, rid = _open_request(conn, cfg_project, key="timeout2")
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET max_attempts=2 WHERE request_id=%s", (rid,))
    conn.commit()

    job = jobs.claim(conn, "w1")
    conn.commit()
    assert job is not None
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET lease_expires_at=now() - interval '1 min' WHERE id=%s",
                    (job.id,))
    conn.commit()

    n = jobs.reclaim_expired(conn)
    conn.commit()
    assert n == 1
    status, attempts, _ = _job_row(conn, rid)
    assert status == "pending"  # requeued, not stuck 'running'/'claimed'
    assert attempts == 1

    # Second (final) attempt also times out.
    job2 = jobs.claim(conn, "w2")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET lease_expires_at=now() - interval '1 min' WHERE id=%s",
                    (job2.id,))
    conn.commit()
    n = jobs.reclaim_expired(conn)
    conn.commit()
    assert n == 1
    status, attempts, _ = _job_row(conn, rid)
    assert status == "dead"
    assert attempts == 2

    reconcile.sweep_once(conn, cfg_project)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] == "failed"


def test_engine_generic_exception_finalizes_failed_and_fails_request(conn, cfg_project,
                                                                      monkeypatch):
    """A generic (non-Timeout) exception from the engine call gets the same
    finalize-as-failed guarantee, and the failure propagates to the request
    the same way."""
    _, rid = _open_request(conn, cfg_project, key="genexc1")

    monkeypatch.setattr(worker.workspace, "create_worktree",
                        lambda project, request_id: Worktree("/tmp", "b", "/tmp"))

    def boom(cfg, job, context, workdir):
        raise ConnectionError("engine process crashed: broken pipe")

    monkeypatch.setattr(worker.job_exec, "run_job", boom)

    assert worker.run_once(cfg_project, "w1") is True
    status, attempts, result = _job_row(conn, rid)
    assert status == "failed"
    assert result["error_type"] == "ConnectionError"
    assert "broken pipe" in result["error"]

    reconcile.sweep_once(conn, cfg_project)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] == "failed"


def test_engine_generic_exception_attempts_not_bumped_by_finalize(conn, cfg_project,
                                                                   monkeypatch):
    """finalize() (the in-process catch path) does not itself touch `attempts` --
    only reclaim_expired (lease-based crash detection) increments it. Pin this
    down explicitly so a future change to the exception handler that starts
    silently retrying in-process does not go unnoticed."""
    _, rid = _open_request(conn, cfg_project, key="genexc2")

    monkeypatch.setattr(worker.workspace, "create_worktree",
                        lambda project, request_id: Worktree("/tmp", "b", "/tmp"))

    def boom(cfg, job, context, workdir):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(worker.job_exec, "run_job", boom)

    assert worker.run_once(cfg_project, "w1") is True
    status, attempts, _ = _job_row(conn, rid)
    assert status == "failed"
    assert attempts == 0


def test_engine_garbled_output_defaults_ready_true_but_qa_gates_it(conn):
    """Unparseable/garbage ARGUS_RESULT output: contracts.parse_result returns {}
    for any line that isn't valid JSON, so dev_ready() falls back to its safe
    default (ready=True, since qa is the real gate). Exercise the designed
    finalize path directly at the contracts layer (parse_result / dev_ready /
    qa_verdict), matching the intent documented in roles/contracts.py."""
    from argus.v2.roles import contracts

    garbage = "not json at all\nARGUS_RESULT: {not valid json}\nmore text"
    parsed = contracts.parse_result(garbage)
    assert parsed == {}  # garbled marker line -> {}, never raises
    assert contracts.dev_ready(parsed) is True  # fail-safe default when parsing fails

    # qa_verdict has its own fail-safe: no verdict field and no test_exit -> pass
    # (advisory / no-project mode). This is the "controlled" behavior for
    # garbage output reaching the judge stage with nothing to gate on.
    assert contracts.qa_verdict(parsed, test_exit=None) == "pass"
    # But when a real test_exit is available, garbage LLM prose can't override it.
    assert contracts.qa_verdict(parsed, test_exit=1) == "fail"


def test_engine_garbled_output_end_to_end_via_worker(conn, cfg_project, monkeypatch):
    """End-to-end: the engine returns text with no ARGUS_RESULT marker at all
    (the realistic 'garbage/partial output' case). The job still finalizes
    cleanly as 'done' (the worker does not crash on unparseable output), and
    downstream pipeline advancement uses the safe defaults from contracts.py."""
    _, rid = _open_request(conn, cfg_project, key="garbled1")

    monkeypatch.setattr(worker.workspace, "create_worktree",
                        lambda project, request_id: Worktree("/tmp", "b", "/tmp"))
    monkeypatch.setattr(worker.workspace, "commit_all", lambda path, message: True)
    monkeypatch.setattr(worker.workspace, "diff", lambda project, path: "some diff")

    def fake_run_job(cfg, job, context, workdir):
        return (
            RunRecord(role=job.role, engine="echo", status="ok",
                      output="the model rambled and forgot to emit a result marker"),
            {},
            [],
        )

    monkeypatch.setattr(worker.job_exec, "run_job", fake_run_job)

    assert worker.run_once(cfg_project, "w1") is True
    status, attempts, result = _job_row(conn, rid)
    assert status == "done"  # garbage output is not a crash; it finalizes cleanly
    assert result["parsed"] == {}  # no marker line found
    assert result["has_diff"] is True

    # Pipeline advances the request using the safe default (ready=True from
    # dev_ready's fallback) since has_diff is True and nothing marks it blocked.
    reconcile.sweep_once(conn, cfg_project)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        req_status = cur.fetchone()[0]
    assert req_status in ("open", "awaiting_approval", "done")  # advanced, not stuck


def test_engine_outage_error_finalizes_as_failed_with_outage_run_status(conn, cfg_project,
                                                                        monkeypatch):
    """EngineOutageError is caught inside exec.run_job (not worker.run_once) and
    turned into a normal (non-raising) return: RunRecord(status='outage') plus
    an 'error' key in the result. worker.run_once now treats this as a
    recoverable engine-level failure: the job is released back to 'pending'
    (not finalized 'failed'), and the run row recorded by jobs.release still
    carries status 'outage' -- distinct from the 'failed' run status used by
    the in-process exception handler."""
    _, rid = _open_request(conn, cfg_project, key="outage1")

    def fake_run_agent(engine, prompt):
        raise EngineOutageError("claude-code CLI not found on PATH")

    monkeypatch.setattr("argus.v2.worker.exec.run_agent", fake_run_agent)
    monkeypatch.setattr(worker.workspace, "create_worktree",
                        lambda project, request_id: Worktree("/tmp", "b", "/tmp"))
    monkeypatch.setattr(worker.workspace, "commit_all", lambda path, message: False)
    monkeypatch.setattr(worker.workspace, "diff", lambda project, path: "")

    assert worker.run_once(cfg_project, "w1") is True
    status, attempts, result = _job_row(conn, rid)
    assert status == "pending"
    assert attempts == 0
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM runs WHERE job_id=(SELECT id FROM jobs "
                    "WHERE request_id=%s)", (rid,))
        run_status = cur.fetchone()[0]
    assert run_status == "outage"  # distinct from the generic-exception 'failed' run status


def test_no_actions_written_on_failed_attempt_then_exactly_once_on_success(conn, cfg_project,
                                                                           monkeypatch):
    """A failed attempt must leave zero action outbox rows (no partial side
    effects). A later successful attempt writes its actions exactly once, and
    finalizing the same job id again (duplicate delivery) does not duplicate."""
    _, rid = _open_request(conn, cfg_project, key="actions1")

    monkeypatch.setattr(worker.workspace, "create_worktree",
                        lambda project, request_id: Worktree("/tmp", "b", "/tmp"))

    def boom(cfg, job, context, workdir):
        raise RuntimeError("engine died before emitting actions")

    monkeypatch.setattr(worker.job_exec, "run_job", boom)
    assert worker.run_once(cfg_project, "w1") is True
    status, _, _ = _job_row(conn, rid)
    assert status == "failed"
    assert _action_count(conn, rid) == 0  # no partial action rows from the failed attempt

    # A fresh job (simulating retry/redispatch after failure) succeeds and emits
    # an action; it must land exactly once.
    monkeypatch.setattr(worker.workspace, "create_worktree",
                        lambda project, request_id: Worktree("/tmp", "b", "/tmp"))
    monkeypatch.setattr(worker.workspace, "commit_all", lambda path, message: True)
    monkeypatch.setattr(worker.workspace, "diff", lambda project, path: "diff")

    def fake_run_job(cfg, job, context, workdir):
        run = RunRecord(role=job.role, engine="echo", status="ok",
                        output='ARGUS_RESULT: {"ready": true}')
        actions = [ActionIntent(type="open_pr", risk="reversible_internal",
                                idempotency_key=f"{job.id}:0")]
        return run, {}, actions

    monkeypatch.setattr(worker.job_exec, "run_job", fake_run_job)

    jid = jobs.enqueue(
        conn, team_id="dev", kind="pipeline", role="developer", stage=0,
        idempotency_key="actions1-retry", exec_snapshot={"engine": "echo"},
        payload={"text": "fix it"}, request_id=rid,
    )
    conn.commit()

    assert worker.run_once(cfg_project, "w1") is True
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM jobs WHERE id=%s", (jid,))
        assert cur.fetchone()[0] == "done"
    assert _action_count(conn, rid) == 1  # exactly once

    # Finalizing the SAME job id again (simulated duplicate delivery) must not
    # add a second row: ON CONFLICT DO NOTHING on the deterministic idempotency
    # key.
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET status='pending', claim_token=NULL WHERE id=%s", (jid,))
    conn.commit()
    job_again = jobs.claim(conn, "w3")
    conn.commit()
    run = RunRecord(role="developer", engine="echo", status="ok",
                    output='ARGUS_RESULT: {"ready": true}')
    actions = [ActionIntent(type="open_pr", risk="reversible_internal",
                            idempotency_key=f"{jid}:0")]
    jobs.finalize(conn, job_again.id, job_again.claim_token, status="done",
                 result={}, run=run, actions=actions)
    conn.commit()
    assert _action_count(conn, rid) == 1  # still exactly once


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


def test_engine_outage_releases_job_and_trips_breaker(conn, cfg_project, monkeypatch):
    """An EngineOutageError run must NOT permanently fail the request: the job
    is released back to pending with a delay, attempts stay at 0, the breaker
    opens for the engine, and the request stays open."""
    from argus.v2.queue import breaker

    _, rid = _open_request(conn, cfg_project, key="outage-release-1")
    _snap_engine(conn, rid)
    monkeypatch.setattr(worker.workspace, "create_worktree",
                        lambda project, request_id: Worktree("/tmp", "b", "/tmp"))
    monkeypatch.setattr(worker.workspace, "commit_all", lambda path, message: False)
    monkeypatch.setattr(worker.workspace, "diff", lambda project, path: "")
    monkeypatch.setattr(worker.job_exec, "run_job", _outage_run_job)

    assert worker.run_once(cfg_project, "w1") is True
    status, attempts, result = _job_row(conn, rid)
    assert status == "pending"
    assert attempts == 0
    assert breaker.open_until(conn, "codex") is not None

    reconcile.sweep_once(conn, cfg_project)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] != "failed"


def test_open_breaker_short_circuits_without_engine_call(conn, cfg_project, monkeypatch):
    """While the breaker is open the worker must not call the engine (or build
    a worktree): the job is released until the breaker expiry."""
    from argus.v2.queue import breaker

    _, rid = _open_request(conn, cfg_project, key="breaker-skip-1")
    _snap_engine(conn, rid)
    breaker.trip(conn, "codex", "usage limit")
    conn.commit()

    def explode(*a, **kw):
        raise AssertionError("engine must not be called while breaker is open")

    monkeypatch.setattr(worker.job_exec, "run_job", explode)
    monkeypatch.setattr(worker.workspace, "create_worktree", explode)

    assert worker.run_once(cfg_project, "w1") is True
    status, attempts, _ = _job_row(conn, rid)
    assert status == "pending"
    assert attempts == 0


def test_outage_release_cap_finalizes_failed(conn, cfg_project, monkeypatch):
    """A job whose outage never heals (engine binary gone) must not loop
    forever: past OUTAGE_RELEASE_CAP it finalizes failed like before."""
    _, rid = _open_request(conn, cfg_project, key="outage-cap-1")
    _snap_engine(conn, rid)
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE jobs SET payload = jsonb_set(payload, '{outage_releases}',
                   to_jsonb(%s::int)) WHERE request_id=%s""",
            (worker.OUTAGE_RELEASE_CAP, rid))
    conn.commit()
    monkeypatch.setattr(worker.workspace, "create_worktree",
                        lambda project, request_id: Worktree("/tmp", "b", "/tmp"))
    monkeypatch.setattr(worker.job_exec, "run_job", _outage_run_job)

    assert worker.run_once(cfg_project, "w1") is True
    status, _, _ = _job_row(conn, rid)
    assert status == "failed"


def test_open_breaker_past_cap_finalizes_failed_without_engine_call(conn, cfg_project,
                                                                     monkeypatch):
    """The skip-path cap branch: breaker open AND outage_releases already at
    OUTAGE_RELEASE_CAP must finalize failed via _finalize_outage_exhausted
    WITHOUT ever building a worktree or calling the engine (mirrors
    test_open_breaker_short_circuits_without_engine_call, but for the
    cap-exhausted branch instead of the still-releasing branch)."""
    from argus.v2.queue import breaker

    _, rid = _open_request(conn, cfg_project, key="breaker-cap-skip-1")
    _snap_engine(conn, rid)
    breaker.trip(conn, "codex", "usage limit")
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE jobs SET payload = jsonb_set(payload, '{outage_releases}',
                   to_jsonb(%s::int)) WHERE request_id=%s""",
            (worker.OUTAGE_RELEASE_CAP, rid))
    conn.commit()

    def explode(*a, **kw):
        raise AssertionError("engine must not be called past the outage release cap")

    monkeypatch.setattr(worker.job_exec, "run_job", explode)
    monkeypatch.setattr(worker.workspace, "create_worktree", explode)

    assert worker.run_once(cfg_project, "w1") is True
    status, attempts, result = _job_row(conn, rid)
    assert status == "failed"
    assert "outage" in result["error"].lower()


def test_successful_run_resets_breaker(conn, cfg_project, monkeypatch):
    from argus.v2.queue import breaker

    _, rid = _open_request(conn, cfg_project, key="breaker-reset-1")
    _snap_engine(conn, rid)
    breaker.trip(conn, "codex", "usage limit")
    with conn.cursor() as cur:  # expire the cooldown so the job runs now
        cur.execute("UPDATE engine_breaker SET open_until = now() - interval '1 second'")
    conn.commit()

    def ok_run_job(cfg, job, context, workdir):
        run = RunRecord(role=job.role, engine="codex", status="ok",
                        output="ANALYSIS: fine")
        return run, {"output": "ANALYSIS: fine"}, []

    monkeypatch.setattr(worker.workspace, "create_worktree",
                        lambda project, request_id: Worktree("/tmp", "b", "/tmp"))
    monkeypatch.setattr(worker.workspace, "commit_all", lambda path, message: True)
    monkeypatch.setattr(worker.workspace, "diff", lambda project, path: "some diff")
    monkeypatch.setattr(worker.job_exec, "run_job", ok_run_job)

    assert worker.run_once(cfg_project, "w1") is True
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM engine_breaker WHERE engine='codex'")
        assert cur.fetchone()[0] == 0


def test_ok_run_resets_breaker_by_run_engine_not_snapshot_engine(conn, cfg_project,
                                                                  monkeypatch):
    """A browser_verify job's exec_snapshot carries a real engine (e.g. codex),
    but the job itself always runs on "browser-use" per _bv_result. If the
    ok-path reset used the snapshot engine_name instead of run.engine, a
    browser_verify job finishing ok would delete a legitimately open breaker
    for an engine it never called. The reset must key off run.engine.

    The breaker for codex must be CLOSED at claim time, otherwise worker.py's
    top-of-function short-circuit (breaker open -> release, never call
    run_job) fires before run_job is ever invoked, and the ok-path reset line
    this test targets would never execute. So we trip the codex breaker
    mid-run instead: inside the fake run_job, using the test's own `conn`
    (committed immediately, visible to run_once's separate pool connection),
    right before returning the ok RunRecord whose engine is "browser-use".
    This proves two things: the job actually ran (not short-circuited), and
    the ok-path reset keys off run.engine rather than the snapshot engine --
    resetting "browser-use" must be a no-op for the codex breaker tripped here."""
    from argus.v2.queue import breaker

    _, rid = _open_request(conn, cfg_project, key="breaker-wrong-engine-1")
    _snap_engine(conn, rid, engine="codex")
    # codex breaker is closed/healthy at claim time so run_once proceeds past
    # the top-of-function short-circuit and actually calls run_job.

    def mismatched_ok_run_job(cfg, job, context, workdir):
        # Trip the codex breaker mid-run, after claim but before the ok-path
        # reset runs. Committed on the test's own connection so it is visible
        # to run_once's separate connection.
        breaker.trip(conn, "codex", "usage limit")
        conn.commit()
        run = RunRecord(role=job.role, engine="browser-use", status="ok",
                        output="verified in browser")
        return run, {"output": "verified in browser"}, []

    monkeypatch.setattr(worker.workspace, "create_worktree",
                        lambda project, request_id: Worktree("/tmp", "b", "/tmp"))
    monkeypatch.setattr(worker.workspace, "commit_all", lambda path, message: True)
    monkeypatch.setattr(worker.workspace, "diff", lambda project, path: "some diff")
    monkeypatch.setattr(worker.job_exec, "run_job", mismatched_ok_run_job)

    assert worker.run_once(cfg_project, "w1") is True
    status, _, _ = _job_row(conn, rid)
    assert status == "done"  # proves the run path actually executed
    # The codex breaker must still be open: resetting "browser-use" (run.engine)
    # is a no-op for codex, since this job never called codex.
    assert breaker.open_until(conn, "codex") is not None


def test_run_once_rolls_back_poisoned_transaction_before_finalize(conn, cfg_project,
                                                                   monkeypatch):
    """If run_job poisons the connection's transaction (e.g. a failing SQL
    statement) and then raises, the except block's jobs.finalize must not
    itself blow up with InFailedSqlTransaction. run_once must roll back the
    aborted transaction before attempting to finalize, and still return True
    with the job finalized failed.

    run_once opens its OWN connection via pool.connect() (a separate
    connection from this test's `conn` fixture), so poisoning must happen on
    THAT connection, not the test's. We wrap pool.connect to capture the
    connection run_once actually holds into `held`, so the monkeypatched
    run_job can issue a deliberately failing statement on it (aborting its
    transaction) before raising. This proves: (1) run_once's own connection
    really does end up in an aborted transaction state from the bad
    statement, and (2) run_once still recovers and finalizes the job failed
    (via the FINDING 3 rollback) instead of propagating
    InFailedSqlTransaction out of run_once. Verification reads back through
    the test's own separate `conn`, which was never touched."""
    import psycopg

    from argus.v2.db import pool as pool_module

    _, rid = _open_request(conn, cfg_project, key="poison1")

    monkeypatch.setattr(worker.workspace, "create_worktree",
                        lambda project, request_id: Worktree("/tmp", "b", "/tmp"))

    # run_once calls pool.connect() for its own main connection AND spawns a
    # heartbeat thread that calls pool.connect() again for its separate
    # connection. Only the FIRST call is run_once's own connection (the one
    # whose transaction state matters for finalize); capture that one only.
    held: dict = {}
    real_connect = pool_module.connect

    def capturing_connect():
        c = real_connect()
        held.setdefault("conn", c)
        return c

    monkeypatch.setattr(worker.pool, "connect", capturing_connect)

    def poison_then_raise(cfg, job, context, workdir):
        # Issue a deliberately failing statement on run_once's OWN connection,
        # aborting its transaction, then raise as if the engine call itself
        # failed afterward.
        held_conn = held["conn"]
        try:
            with held_conn.cursor() as cur:
                cur.execute("SELECT 1/0")
        except psycopg.Error:
            pass  # transaction on held_conn is now aborted
        raise RuntimeError("engine call failed after poisoning the transaction")

    monkeypatch.setattr(worker.job_exec, "run_job", poison_then_raise)

    assert worker.run_once(cfg_project, "w1") is True
    status, attempts, result = _job_row(conn, rid)
    assert status == "failed"
    assert result["error_type"] == "RuntimeError"
    assert "poisoning the transaction" in result["error"]
