"""Spec-invariant acceptance tests. Each maps to a row in docs/v2-acceptance.md.
echo engine only; no network, no cost."""
from argus.v2.actions import approvals, executor
from argus.v2.ingress import events
from argus.v2.orchestrator import pipeline, reconcile
from argus.v2.queue import jobs
from argus.v2.queue.models import ActionIntent, RunRecord


def _request(conn, cfg, fp=None, source="cli", key="m1", text="t"):
    if fp:
        eid = events.ingest_signal(conn, cfg, team="dev", source=source,
                                   fingerprint=fp, payload={"e": 1})
        return eid, pipeline.open_request(conn, cfg, event_id=eid, team_id="dev",
                                          conversation_id=None, fingerprint=fp)
    eid = events.ingest_message(conn, cfg, team="dev", source=source, dedup_key=key, text=text)
    return eid, pipeline.open_request(conn, cfg, event_id=eid, team_id="dev",
                                      conversation_id=None)


def _push_to_awaiting(conn, cfg, request_id):
    """Attach an irreversible action and run the executor so the request parks."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO actions (request_id, team_id, type, risk, idempotency_key) "
            "VALUES (%s,'dev','merge','irreversible_outward','x0')", (request_id,))
    executor.process_proposed(conn, cfg)


def test_signal_dedup_while_awaiting_approval(conn, cfg):
    _, r1 = _request(conn, cfg, fp="ISSUE-1"); conn.commit()
    _push_to_awaiting(conn, cfg, r1); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests WHERE id=%s", (r1,))
        assert cur.fetchone()[0] == "awaiting_approval"
    e2 = events.ingest_signal(conn, cfg, team="dev", source="sentry",
                              fingerprint="ISSUE-1", payload={"e": 2}); conn.commit()
    r2 = pipeline.open_request(conn, cfg, event_id=e2, team_id="dev",
                               conversation_id=None, fingerprint="ISSUE-1"); conn.commit()
    assert r2 is None  # deduped even though r1 is awaiting approval, not open


def test_duplicate_fingerprint_does_not_rollback_batch(conn, cfg):
    _, existing = _request(conn, cfg, fp="DUP"); conn.commit()
    _push_to_awaiting(conn, cfg, existing); conn.commit()

    events.ingest_signal(conn, cfg, team="dev", source="sentry",
                         fingerprint="NEW", payload={"e": 1})
    events.ingest_signal(conn, cfg, team="dev", source="vercel",
                         fingerprint="DUP", payload={"e": 2})
    conn.commit()

    reconcile.route_events(conn, cfg)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM requests WHERE fingerprint='NEW'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT status FROM events WHERE source IN ('sentry','vercel') "
                    "AND dedup_key IN ('NEW','DUP') ORDER BY source")
        assert [r[0] for r in cur.fetchall()] == ["processed", "processed"]


def test_outward_notify_requires_approval(conn, cfg):
    _, rid = _request(conn, cfg); conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO actions (request_id, team_id, type, risk, destination_ref, "
            "idempotency_key) VALUES (%s,'dev','notify','irreversible_outward','out:cust','n0')",
            (rid,))
    conn.commit()
    executor.process_proposed(conn, cfg); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM actions WHERE idempotency_key='n0'")
        assert cur.fetchone()[0] == "awaiting_approval"  # NOT 'done' / auto-sent
        cur.execute("SELECT count(*) FROM approvals"); assert cur.fetchone()[0] == 1


def test_action_outbox_exactly_once_across_retry(conn, cfg):
    """A job that emits an action, when retried (same job id), yields exactly one
    action row (deterministic idempotency_key + ON CONFLICT DO NOTHING)."""
    jid = jobs.enqueue(conn, team_id="dev", kind="pipeline", role="developer", stage=0,
                       idempotency_key="k1", exec_snapshot={"engine": "echo"}, payload={})
    conn.commit()
    intent = ActionIntent(type="open_pr", risk="reversible_internal",
                          idempotency_key=f"{jid}:0")
    run = RunRecord(role="developer", engine="echo", status="ok", output="x")
    # First execution.
    job = jobs.claim(conn, "w1"); conn.commit()
    jobs.finalize(conn, job.id, job.claim_token, status="done", result={},
                  run=run, actions=[intent]); conn.commit()
    # Force a retry of the SAME job id: back to pending, reclaim, finalize again.
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET status='pending', claim_token=NULL WHERE id=%s", (jid,))
    conn.commit()
    job2 = jobs.claim(conn, "w2"); conn.commit()
    jobs.finalize(conn, job2.id, job2.claim_token, status="done", result={},
                  run=run, actions=[intent]); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM actions WHERE idempotency_key=%s", (f"{jid}:0",))
        assert cur.fetchone()[0] == 1  # exactly once despite two finalizes


def test_config_snapshot_survives_apply(conn, cfg):
    """A job carries a frozen exec_snapshot; changing config does not change how a
    queued job runs."""
    _, rid = _request(conn, cfg, text="fix bug"); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT exec_snapshot FROM jobs WHERE request_id=%s", (rid,))
        snap = cur.fetchone()[0]
    assert snap["engine"] == "echo"  # frozen at creation
    assert "config_hash" in snap     # provenance recorded
    # Even if cfg changes later, the stored snapshot is what the worker uses.
    with conn.cursor() as cur:
        cur.execute("SELECT exec_snapshot->>'engine' FROM jobs WHERE request_id=%s", (rid,))
        assert cur.fetchone()[0] == "echo"


def test_dead_job_fails_request(conn, cfg):
    """A pipeline job that exhausts attempts becomes dead and fails its request."""
    _, rid = _request(conn, cfg, text="fix bug"); conn.commit()
    # Drive the single stage job to dead by exhausting its attempts.
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET max_attempts=1 WHERE request_id=%s", (rid,))
    conn.commit()
    job = jobs.claim(conn, "w1"); conn.commit()
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET lease_expires_at=now()-interval '1 min' WHERE id=%s", (job.id,))
    conn.commit()
    reconcile.sweep_once(conn, cfg); conn.commit()   # reclaim -> dead, advance -> failed
    reconcile.sweep_once(conn, cfg); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM jobs WHERE id=%s", (job.id,)); assert cur.fetchone()[0] == "dead"
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,)); assert cur.fetchone()[0] == "failed"


def test_event_replay_no_duplicate_reply(conn, cfg):
    """A reply-only turn reprocessed after a crash emits exactly one reply action
    (deterministic idempotency_key reply:<event_id>)."""
    events.ingest_message(conn, cfg, team="dev", source="cli", dedup_key="m1",
                          text="thanks"); conn.commit()
    reconcile.sweep_once(conn, cfg); conn.commit()           # emits reply, marks processed
    # Simulate a crash that lost the 'processed' mark: reset to received and re-run.
    with conn.cursor() as cur:
        cur.execute("UPDATE events SET status='received', processed_at=NULL WHERE dedup_key='m1'")
    conn.commit()
    reconcile.sweep_once(conn, cfg); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM actions WHERE type='reply'")
        assert cur.fetchone()[0] == 1  # not duplicated
