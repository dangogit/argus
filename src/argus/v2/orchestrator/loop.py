"""Orchestrator main loop. One active orchestrator via a Postgres advisory
lock (defense in depth; correctness does not depend on it). Wakes on NOTIFY,
falls back to a poll interval.

Reliability: a single sweep raising must NOT take the orchestrator down. Each
sweep runs on its own connection inside `_sweep`, which logs and swallows the
error and returns False; the loop then backs off and keeps going. Without this,
one bad sweep exits `run()` with no trace and the whole pipeline silently stalls.
"""
from __future__ import annotations

import logging
import select

from argus.v2.db import pool
from argus.v2.orchestrator import reconcile

log = logging.getLogger("argus.orchestrator")

ADVISORY_KEY = 770_011  # arbitrary, stable

# Backoff (seconds) indexed by consecutive-failure count. Caps the storm when a
# dependency (DB, provider) is down so a failing sweep does not hot-loop.
_BACKOFF = (1.0, 2.0, 5.0, 15.0, 30.0)


def _backoff_seconds(failures: int) -> float:
    """Seconds to wait given N consecutive sweep failures. 0 when healthy."""
    if failures <= 0:
        return 0.0
    return _BACKOFF[min(failures, len(_BACKOFF)) - 1]


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


def run(cfg, *, poll_seconds: float = 2.0, max_iterations: int | None = None) -> None:
    conn = pool.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_KEY,))
        if not cur.fetchone()[0]:
            conn.close()
            raise RuntimeError("another orchestrator holds the advisory lock")
        cur.execute("LISTEN argus_jobs; LISTEN argus_actions;")
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
            select.select([conn], [], [], timeout)
            conn.notifies()  # drain
            iterations += 1
    finally:
        conn.close()
        log.info("orchestrator stopped after %d iteration(s)", iterations)
