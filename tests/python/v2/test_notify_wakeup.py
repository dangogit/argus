"""Trigger-driven NOTIFY wakeups (migration 0021). Without these, the
orchestrator loop's LISTEN never fires and it silently degrades to the 2s
poll. A LISTENing connection must see a notification promptly after a job,
action, or event write, and loop._wait must return True before the timeout."""
from __future__ import annotations

import select
import time

import psycopg
import pytest

from argus.v2.orchestrator import loop

WAIT_TIMEOUT = 5.0


def _listen(pg_dsn, *channels: str) -> psycopg.Connection:
    listener = psycopg.connect(pg_dsn, autocommit=True)
    with listener.cursor() as cur:
        for ch in channels:
            cur.execute(f"LISTEN {ch}")
    return listener


def _wait_for_notify(listener: psycopg.Connection, timeout: float = WAIT_TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if select.select([listener], [], [], deadline - time.monotonic())[0]:
            notifies = listener.notifies(timeout=0)
            if notifies:
                return list(notifies)
    return []


def _insert_job(conn, key: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO jobs (team_id, role, kind, idempotency_key, exec_snapshot, payload)
            VALUES (%s, %s, %s, %s, '{}', '{}')
            RETURNING id
            """,
            ("dev", "developer", "pipeline", key),
        )
        return cur.fetchone()[0]


def _insert_action(conn, key: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO actions (team_id, type, risk, idempotency_key)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            ("dev", "notify", "reversible_internal", key),
        )
        return cur.fetchone()[0]


def _insert_event(conn, key: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO events (team_id, kind, source, dedup_key)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            ("dev", "signal", "test", key),
        )
        return cur.fetchone()[0]


def test_job_insert_notifies(pg_dsn, conn):
    listener = _listen(pg_dsn, "argus_jobs")
    try:
        _insert_job(conn, "notify-job-insert")
        conn.commit()
        assert _wait_for_notify(listener), "expected NOTIFY on argus_jobs after job insert"
    finally:
        listener.close()


def test_job_status_update_notifies(pg_dsn, conn):
    job_id = _insert_job(conn, "notify-job-update")
    conn.commit()
    listener = _listen(pg_dsn, "argus_jobs")
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE jobs SET status='claimed' WHERE id=%s", (job_id,))
        conn.commit()
        assert _wait_for_notify(listener), "expected NOTIFY on argus_jobs after status update"
    finally:
        listener.close()


def test_action_insert_notifies(pg_dsn, conn):
    listener = _listen(pg_dsn, "argus_actions")
    try:
        _insert_action(conn, "notify-action-insert")
        conn.commit()
        assert _wait_for_notify(listener), "expected NOTIFY on argus_actions after action insert"
    finally:
        listener.close()


def test_action_status_update_notifies(pg_dsn, conn):
    action_id = _insert_action(conn, "notify-action-update")
    conn.commit()
    listener = _listen(pg_dsn, "argus_actions")
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE actions SET status='approved' WHERE id=%s", (action_id,))
        conn.commit()
        assert _wait_for_notify(listener), "expected NOTIFY on argus_actions after status update"
    finally:
        listener.close()


def test_event_insert_notifies(pg_dsn, conn):
    listener = _listen(pg_dsn, "argus_events")
    try:
        _insert_event(conn, "notify-event-insert")
        conn.commit()
        assert _wait_for_notify(listener), "expected NOTIFY on argus_events after event insert"
    finally:
        listener.close()


def test_loop_wait_returns_promptly_on_job_insert(pg_dsn, conn, monkeypatch):
    """End-to-end: loop._wait must observe the NOTIFY well before the 2s poll
    timeout instead of blocking to the deadline."""
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)
    control = psycopg.connect(pg_dsn, autocommit=True)
    with control.cursor() as cur:
        cur.execute("LISTEN argus_jobs")
    try:
        start = time.monotonic()
        # Insert on a separate connection so the NOTIFY is delivered to `control`
        # only after commit, same as production traffic.
        _insert_job(conn, "notify-loop-wait")
        conn.commit()
        healthy = loop._wait(control, timeout=2.0)
        elapsed = time.monotonic() - start
        assert healthy is True
        assert elapsed < 1.5, f"loop._wait took {elapsed}s, expected a prompt NOTIFY wakeup"
    finally:
        control.close()
