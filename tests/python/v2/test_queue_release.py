"""jobs.release: put a claimed job back to pending with a delay WITHOUT
incrementing attempts. Used for engine outages, where the failure belongs to
the engine, not the job. Fenced by claim_token like finalize."""
from __future__ import annotations

from argus.v2.queue import jobs
from argus.v2.queue.models import RunRecord


def _enqueue(conn, key="rel1"):
    jid = jobs.enqueue(conn, team_id="dev", kind="pipeline", role="developer",
                       stage=0, idempotency_key=key,
                       exec_snapshot={"engine": "codex"}, payload={"text": "x"})
    conn.commit()
    return jid


def _run(status="outage"):
    return RunRecord(role="developer", engine="codex", status=status,
                     prompt="p", output="usage limit hit")


def _row(conn, jid):
    with conn.cursor() as cur:
        cur.execute("""SELECT status, attempts, claim_token,
                              (payload->>'outage_releases')::int,
                              run_after > now() + interval '30 seconds'
                       FROM jobs WHERE id=%s""", (jid,))
        return cur.fetchone()


def test_release_requeues_with_delay_without_burning_attempt(conn):
    jid = _enqueue(conn)
    job = jobs.claim(conn, "w1")
    conn.commit()
    assert job is not None and job.id == jid

    ok = jobs.release(conn, job.id, job.claim_token, delay_seconds=120,
                      run=_run(), reason="engine outage")
    conn.commit()
    assert ok is True
    status, attempts, token, releases, delayed = _row(conn, jid)
    assert status == "pending"
    assert attempts == 0          # NOT incremented
    assert token is None
    assert releases == 1
    assert delayed is True        # run_after pushed into the future

    # Not claimable until run_after passes.
    assert jobs.claim(conn, "w2") is None


def test_release_increments_payload_counter_across_releases(conn):
    jid = _enqueue(conn, key="rel2")
    for expected in (1, 2):
        with conn.cursor() as cur:  # make it claimable immediately
            cur.execute("UPDATE jobs SET run_after=now() WHERE id=%s", (jid,))
        conn.commit()
        job = jobs.claim(conn, "w1")
        conn.commit()
        assert job is not None
        jobs.release(conn, job.id, job.claim_token, delay_seconds=1,
                     run=_run(), reason="engine outage")
        conn.commit()
        assert _row(conn, jid)[3] == expected
    # counter is visible to the next claimer via job.payload
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET run_after=now() WHERE id=%s", (jid,))
    conn.commit()
    job = jobs.claim(conn, "w1")
    assert (job.payload or {}).get("outage_releases") == 2


def test_release_is_fenced_by_claim_token(conn):
    jid = _enqueue(conn, key="rel3")
    job = jobs.claim(conn, "w1")
    conn.commit()
    ok = jobs.release(conn, job.id, "00000000-0000-0000-0000-000000000000",
                      delay_seconds=60, run=_run(), reason="stale")
    conn.commit()
    assert ok is False
    status = _row(conn, jid)[0]
    assert status == "claimed"    # untouched by the stale caller
