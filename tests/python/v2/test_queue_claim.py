from argus.v2.queue import jobs


def _enqueue(conn, key, run_after=None):
    return jobs.enqueue(conn, team_id="dev", kind="pipeline", role="developer",
                        stage=0, idempotency_key=key, exec_snapshot={"engine": "echo"},
                        payload={"text": "hi"}, run_after=run_after)


def test_enqueue_is_idempotent_on_key(conn):
    a = _enqueue(conn, "k1")
    conn.commit()
    b = _enqueue(conn, "k1")
    conn.commit()
    assert a == b  # ON CONFLICT returns the existing id


def test_claim_returns_one_job_with_token(conn):
    _enqueue(conn, "k1")
    conn.commit()
    job = jobs.claim(conn, "w1", lease_seconds=60)
    conn.commit()
    assert job is not None
    assert job.role == "developer"
    assert job.claim_token is not None
    assert job.status == "claimed"


def test_claim_skips_already_claimed(conn):
    _enqueue(conn, "k1")
    conn.commit()
    first = jobs.claim(conn, "w1", lease_seconds=60); conn.commit()
    second = jobs.claim(conn, "w2", lease_seconds=60); conn.commit()
    assert first is not None and second is None


def test_claim_respects_run_after(conn):
    from datetime import datetime, timedelta, timezone
    _enqueue(conn, "k1", run_after=datetime.now(timezone.utc) + timedelta(hours=1))
    conn.commit()
    assert jobs.claim(conn, "w1", lease_seconds=60) is None


def test_claim_prioritizes_converse_and_triage_over_pipeline(conn):
    # A pipeline job enqueued FIRST must not be claimed before an owner-facing
    # converse job enqueued later (chat must not wait behind a monitoring flood).
    jobs.enqueue(conn, team_id="dev", kind="pipeline", role="developer", stage=0,
                 idempotency_key="p1", exec_snapshot={"engine": "echo"}, payload={})
    jobs.enqueue(conn, team_id="dev", kind="research", role="researcher", stage=0,
                 idempotency_key="r1", exec_snapshot={"engine": "echo"}, payload={})
    jobs.enqueue(conn, team_id="dev", kind="triage", role="manager", stage=0,
                 idempotency_key="t1", exec_snapshot={"engine": "echo"}, payload={})
    jobs.enqueue(conn, team_id="dev", kind="converse", role="manager", stage=0,
                 idempotency_key="c1", exec_snapshot={"engine": "echo"}, payload={})
    conn.commit()
    order = []
    while (j := jobs.claim(conn, "w1", lease_seconds=60)) is not None:
        order.append(j.kind); conn.commit()
    assert order[0] == "converse"      # chat first
    assert order[1] == "triage"        # then signal triage
    assert set(order[2:]) == {"pipeline", "research"}  # background last
