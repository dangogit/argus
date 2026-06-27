"""Full loop on the echo engine: a message dispatches a request that runs the
manager-less pipeline (developer->qa->senior) to done, driven only by the
orchestrator sweep + worker drain."""
from argus.v2.ingress import events
from argus.v2.orchestrator import reconcile
from argus.v2.worker import worker


def _drain(conn, cfg, rounds=12):
    for _ in range(rounds):
        reconcile.sweep_once(conn, cfg); conn.commit()
        while worker.run_once(cfg, "w1"):
            pass


def test_message_runs_pipeline_to_done(conn, cfg):
    events.ingest_message(conn, cfg, team="dev", source="cli", dedup_key="m1",
                          text="fix the login bug"); conn.commit()
    _drain(conn, cfg)
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests"); assert cur.fetchone()[0] == "done"
        cur.execute("SELECT role FROM jobs ORDER BY stage")
        assert [r[0] for r in cur.fetchall()] == ["developer", "qa", "senior"]
        cur.execute("SELECT count(*) FROM runs"); assert cur.fetchone()[0] == 3


def test_signal_runs_pipeline_to_done(conn, cfg):
    events.ingest_signal(conn, cfg, team="dev", source="sentry",
                         fingerprint="ISSUE-1", payload={"e": 1}); conn.commit()
    _drain(conn, cfg)
    with conn.cursor() as cur:
        cur.execute("SELECT status, fingerprint FROM requests")
        status, fp = cur.fetchone()
    assert status == "done" and fp == "ISSUE-1"


def test_crashed_worker_job_is_recovered(conn, cfg):
    from argus.v2.queue import jobs
    events.ingest_message(conn, cfg, team="dev", source="cli", dedup_key="m1",
                          text="fix the login bug"); conn.commit()
    reconcile.sweep_once(conn, cfg); conn.commit()  # opens request + stage 0 job
    job = jobs.claim(conn, "w1"); conn.commit()      # claimed, then "crash" (never finalize)
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET lease_expires_at=now()-interval '1 min' WHERE id=%s", (job.id,))
    conn.commit()
    _drain(conn, cfg)  # reclaim + complete
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests"); assert cur.fetchone()[0] == "done"
