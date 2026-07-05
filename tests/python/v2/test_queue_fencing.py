from pathlib import Path

import psycopg
import pytest

from argus.v2.queue import jobs
from argus.v2.queue.models import RunRecord

_RUNS_UNIQUE_MIGRATION = (Path(__file__).resolve().parents[3] / "src" / "argus"
                          / "v2" / "db" / "migrations" / "0022_runs_unique.sql")


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


def test_runs_unique_migration_dedupes_existing_duplicates(conn):
    """The migration must survive a live database that already has duplicate
    (job_id, attempt) rows: it dedupes first (keeping the earliest row), then
    adds the constraint. Replayed here by dropping the constraint, seeding
    duplicates, and re-running the migration file. DDL is transactional in
    Postgres, so a failure rolls back and later tests see the original schema."""
    _enqueue(conn); conn.commit()
    job = jobs.claim(conn, "w1"); conn.commit()
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE runs DROP CONSTRAINT runs_job_id_attempt_key")
        cur.execute(
            "INSERT INTO runs (job_id, attempt, claim_token, role, engine, status,"
            " started_at) VALUES (%s, 0, %s, 'developer', 'echo', 'ok',"
            " now() - interval '2 min') RETURNING id",
            (job.id, job.claim_token),
        )
        earliest_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO runs (job_id, attempt, claim_token, role, engine, status,"
            " started_at) VALUES (%s, 0, %s, 'developer', 'echo', 'ok', now())",
            (job.id, job.claim_token),
        )
        cur.execute(_RUNS_UNIQUE_MIGRATION.read_text(encoding="utf-8"))
        cur.execute("SELECT id FROM runs WHERE job_id=%s AND attempt=0", (job.id,))
        rows = cur.fetchall()
    conn.commit()
    assert rows == [(earliest_id,)]  # duplicates gone, earliest row kept


def test_runs_unique_constraint_rejects_duplicate_job_id_attempt(conn):
    """A second run row for the same (job_id, attempt) is a phantom audit row
    from a double-claimed job; the DB must reject it directly."""
    _enqueue(conn); conn.commit()
    job = jobs.claim(conn, "w1"); conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO runs (job_id, attempt, claim_token, role, engine, status)"
            " VALUES (%s, 0, %s, 'developer', 'echo', 'ok')",
            (job.id, job.claim_token),
        )
    conn.commit()
    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO runs (job_id, attempt, claim_token, role, engine, status)"
                " VALUES (%s, 0, %s, 'developer', 'echo', 'ok')",
                (job.id, job.claim_token),
            )
    conn.rollback()


def test_finalize_insert_tolerates_conflicting_run_row(conn):
    """If a run row for this (job_id, attempt) already exists (e.g. a stray
    duplicate written by a since-fenced-out worker), finalize's insert must
    not blow up the whole finalize transaction: ON CONFLICT DO NOTHING."""
    _enqueue(conn); conn.commit()
    job = jobs.claim(conn, "w1"); conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO runs (job_id, attempt, claim_token, role, engine, status)"
            " VALUES (%s, 0, %s, 'developer', 'echo', 'ok')",
            (job.id, job.claim_token),
        )
    conn.commit()
    ok = jobs.finalize(conn, job.id, job.claim_token, status="done",
                       result={"r": 1}, run=_run(), actions=[])
    conn.commit()
    assert ok is True
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM jobs WHERE id=%s", (job.id,))
        assert cur.fetchone()[0] == "done"
        cur.execute("SELECT count(*) FROM runs WHERE job_id=%s AND attempt=0", (job.id,))
        assert cur.fetchone()[0] == 1  # still just one row, not two
