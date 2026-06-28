"""Orchestrator main loop. One active orchestrator via a Postgres advisory
lock (defense in depth; correctness does not depend on it). Wakes on NOTIFY,
falls back to a poll interval.

Reliability:
- A single sweep raising must NOT take the orchestrator down. Each sweep runs on
  its own connection inside `_sweep`, which logs and swallows the error and
  returns False; the loop then backs off and keeps going.
- The control connection holds the advisory lock and the LISTEN registration.
  If it drops, the server releases the lock and NOTIFY wakeups stop. The loop
  detects the drop and re-acquires the lock + re-LISTENs. If another orchestrator
  has taken the lock in the meantime, re-acquire fails and this one exits - a
  correct handoff, not a silent double-run.
"""
from __future__ import annotations

import logging
import select
import time

import psycopg

from argus.v2.db import pool
from argus.v2.orchestrator import reconcile

log = logging.getLogger("argus.orchestrator")

ADVISORY_KEY = 770_011  # arbitrary, stable

# Backoff (seconds) indexed by consecutive-failure count. Caps the storm when a
# dependency (DB, provider) is down so a failing sweep does not hot-loop.
_BACKOFF = (1.0, 2.0, 5.0, 15.0, 30.0)

# Reconnect attempts after the control connection drops, before giving up and
# letting the supervisor (launchd/systemd) restart the process.
_RECONNECT_TRIES = 10

_DROP_ERRORS = (psycopg.OperationalError, psycopg.InterfaceError, OSError, ValueError)


def _backoff_seconds(failures: int) -> float:
    """Seconds to wait given N consecutive sweep failures. 0 when healthy."""
    if failures <= 0:
        return 0.0
    return _BACKOFF[min(failures, len(_BACKOFF)) - 1]


def _reconnect_backoff(attempt: int) -> float:
    return min(2.0 ** attempt, 30.0)


def _sweep(cfg) -> bool:
    """Run one sweep on its own connection. Returns True on success. Never
    raises: a sweep crash is logged and the orchestrator keeps running."""
    work = pool.connect()
    try:
        reconcile.sweep_once(work, cfg)
        work.commit()
        return True
    except Exception:
        log.exception("orchestrator sweep failed")
        try:
            work.rollback()
        except Exception:
            pass
        return False
    finally:
        work.close()


def _acquire():
    """Open the control connection, take the advisory lock, and register LISTEN.
    Raises RuntimeError if another orchestrator already holds the lock."""
    conn = pool.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_KEY,))
        if not cur.fetchone()[0]:
            conn.close()
            raise RuntimeError("another orchestrator holds the advisory lock")
        cur.execute("LISTEN argus_jobs; LISTEN argus_actions;")
    return conn


def _reacquire():
    """Re-establish the control connection after a drop. Retries while the DB is
    unreachable; propagates RuntimeError immediately when another orchestrator
    has taken the lock (a handoff - this process must exit, not double-run)."""
    last = None
    for attempt in range(_RECONNECT_TRIES):
        try:
            return _acquire()
        except RuntimeError:
            raise  # another orchestrator is leader now; stop.
        except _DROP_ERRORS as exc:
            last = exc
            log.warning("reconnect attempt %d failed: %s", attempt + 1, exc)
            time.sleep(_reconnect_backoff(attempt))
    raise last if last else RuntimeError("orchestrator could not reconnect")


def _wait(conn, timeout: float) -> bool:
    """Block until a NOTIFY arrives or `timeout` elapses. Returns True if the
    control connection is still healthy, False if it dropped (caller reconnects)."""
    try:
        select.select([conn], [], [], timeout)
        conn.notifies()  # drain; touches the socket so a dead conn surfaces here
        return True
    except _DROP_ERRORS as exc:
        log.warning("orchestrator control connection dropped: %s", exc)
        return False


def run(cfg, *, poll_seconds: float = 2.0, max_iterations: int | None = None) -> None:
    conn = _acquire()
    log.info("orchestrator started (poll_seconds=%s)", poll_seconds)
    iterations = 0
    failures = 0
    try:
        while max_iterations is None or iterations < max_iterations:
            if _sweep(cfg):
                failures = 0
            else:
                failures += 1
                log.warning("sweep failed (%d in a row), backing off", failures)
            # On failure, wait at least the backoff; otherwise the normal poll.
            timeout = max(poll_seconds, _backoff_seconds(failures))
            if not _wait(conn, timeout):
                try:
                    conn.close()
                except Exception:
                    pass
                conn = _reacquire()  # raises (handoff or DB gone) -> process exits
                log.info("orchestrator control connection re-established")
            iterations += 1
    finally:
        try:
            conn.close()
        except Exception:
            pass
        log.info("orchestrator stopped after %d iteration(s)", iterations)
