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


def _enqueue_kind(conn, kind, key):
    return jobs.enqueue(conn, team_id="dev", kind=kind, role="manager", stage=0,
                        idempotency_key=key, exec_snapshot={"engine": "echo"}, payload={})


def test_claim_chat_lane_takes_only_chat_kinds(conn):
    """include_kinds (chat lane) claims only converse/triage; it never picks up a
    pipeline job even when one is the only thing left."""
    _enqueue_kind(conn, "pipeline", "p1")
    _enqueue_kind(conn, "converse", "c1")
    conn.commit()
    j = jobs.claim(conn, "chat", include_kinds=list(jobs.CHAT_KINDS)); conn.commit()
    assert j is not None and j.kind == "converse"
    # Only the pipeline job remains -> chat lane claims nothing.
    assert jobs.claim(conn, "chat", include_kinds=list(jobs.CHAT_KINDS)) is None


def test_claim_empty_include_kinds_claims_nothing(conn):
    """An empty include lane must claim NOTHING, not fall through to every job."""
    _enqueue_kind(conn, "pipeline", "p1")
    conn.commit()
    assert jobs.claim(conn, "chat", include_kinds=[]) is None
    # Sanity: None (no filter) still claims it.
    assert jobs.claim(conn, "w1", include_kinds=None) is not None


def test_claim_pipeline_lane_excludes_chat_kinds(conn):
    """exclude_kinds (pipeline lane) skips chat jobs and takes the rest, so a slow
    build lane never starves a converse reply by grabbing it."""
    _enqueue_kind(conn, "converse", "c1")
    _enqueue_kind(conn, "pipeline", "p1")
    _enqueue_kind(conn, "research", "r1")
    conn.commit()
    kinds = []
    while (j := jobs.claim(conn, "pipe", exclude_kinds=list(jobs.CHAT_KINDS))) is not None:
        kinds.append(j.kind); conn.commit()
    assert set(kinds) == {"pipeline", "research"}
    assert "converse" not in kinds  # left for the chat lane
    # The converse job is still pending for the chat lane.
    assert jobs.claim(conn, "chat", include_kinds=list(jobs.CHAT_KINDS)).kind == "converse"


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
