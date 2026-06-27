from argus.v2.queue import jobs
from argus.v2.queue.models import RunRecord


def _enqueue(conn, key="k1"):
    return jobs.enqueue(conn, team_id="dev", kind="pipeline", role="developer",
                        stage=0, idempotency_key=key, exec_snapshot={"engine": "echo"},
                        payload={})


def _run(status="ok"):
    return RunRecord(role="developer", engine="echo", status=status, output="x")


def test_finalize_with_correct_token_succeeds(conn):
    _enqueue(conn); conn.commit()
    job = jobs.claim(conn, "w1"); conn.commit()
    ok = jobs.finalize(conn, job.id, job.claim_token, status="done",
                       result={"r": 1}, run=_run(), actions=[])
    conn.commit()
    assert ok is True
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM jobs WHERE id=%s", (job.id,))
        assert cur.fetchone()[0] == "done"
        cur.execute("SELECT count(*) FROM runs WHERE job_id=%s", (job.id,))
        assert cur.fetchone()[0] == 1


def test_finalize_with_stale_token_is_rejected(conn):
    _enqueue(conn); conn.commit()
    job = jobs.claim(conn, "w1"); conn.commit()
    stale = job.claim_token
    # Simulate reclaim: lease expires, another worker reclaims (token rotates).
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET lease_expires_at=now() - interval '1 min' WHERE id=%s", (job.id,))
    conn.commit()
    jobs.reclaim_expired(conn); conn.commit()
    job2 = jobs.claim(conn, "w2"); conn.commit()
    assert job2.claim_token != stale
    # Zombie worker w1 tries to finalize with its old token.
    ok = jobs.finalize(conn, job.id, stale, status="done", result={}, run=_run(), actions=[])
    conn.commit()
    assert ok is False
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM jobs WHERE id=%s", (job.id,))
        assert cur.fetchone()[0] == "claimed"  # still owned by w2, not done


def test_heartbeat_extends_only_for_owner(conn):
    _enqueue(conn); conn.commit()
    job = jobs.claim(conn, "w1"); conn.commit()
    assert jobs.heartbeat(conn, job.id, job.claim_token) is True
    assert jobs.heartbeat(conn, job.id, "00000000-0000-0000-0000-000000000000") is False


def test_reclaim_requeues_and_bumps_attempts(conn):
    _enqueue(conn); conn.commit()
    job = jobs.claim(conn, "w1"); conn.commit()
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET lease_expires_at=now() - interval '1 min' WHERE id=%s", (job.id,))
    conn.commit()
    n = jobs.reclaim_expired(conn); conn.commit()
    assert n == 1
    with conn.cursor() as cur:
        cur.execute("SELECT status, attempts FROM jobs WHERE id=%s", (job.id,))
        status, attempts = cur.fetchone()
    assert status == "pending" and attempts == 1


def test_reclaim_to_dead_after_max_attempts(conn):
    jobs.enqueue(conn, team_id="dev", kind="pipeline", role="developer", stage=0,
                 idempotency_key="k", exec_snapshot={}, payload={}, max_attempts=1)
    conn.commit()
    job = jobs.claim(conn, "w1"); conn.commit()
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET lease_expires_at=now() - interval '1 min' WHERE id=%s", (job.id,))
    conn.commit()
    jobs.reclaim_expired(conn); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM jobs WHERE id=%s", (job.id,))
        assert cur.fetchone()[0] == "dead"
