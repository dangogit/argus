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
   key in the result dict. worker.run_once then finalizes the job with
   status="failed" (run.status != "ok"; see run_once:
   `status = "done" if run.status == "ok" else "failed"`).
   So an outage also ends up 'failed', but via the normal finalize path
   (with a job result attached), not the exception handler.

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
    an 'error' key in the result. worker.run_once then finalizes the JOB as
    'failed' (run.status != 'ok'), and the run row itself records status
    'outage' as designed -- distinct from the 'failed' run status used by the
    in-process exception handler."""
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
    assert status == "failed"
    assert result["error"] == "claude-code CLI not found on PATH"
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
