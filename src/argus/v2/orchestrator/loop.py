"""Orchestrator main loop. One active orchestrator via a Postgres advisory
lock (defense in depth; correctness does not depend on it). Wakes on NOTIFY,
falls back to a poll interval."""
from __future__ import annotations

import select
import time

from argus.v2.db import pool
from argus.v2.orchestrator import reconcile

ADVISORY_KEY = 770_011  # arbitrary, stable


def run(cfg, *, poll_seconds: float = 2.0, max_iterations: int | None = None) -> None:
    conn = pool.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_KEY,))
        if not cur.fetchone()[0]:
            conn.close()
            raise RuntimeError("another orchestrator holds the advisory lock")
        cur.execute("LISTEN argus_jobs; LISTEN argus_actions;")
    iterations = 0
    try:
        while max_iterations is None or iterations < max_iterations:
            work = pool.connect()
            try:
                reconcile.sweep_once(work, cfg)
                work.commit()
            finally:
                work.close()
            select.select([conn], [], [], poll_seconds)
            conn.notifies()  # drain
            iterations += 1
    finally:
        conn.close()
