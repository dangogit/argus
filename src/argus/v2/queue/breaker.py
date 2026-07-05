"""Engine circuit breaker. An engine outage (usage limit, auth failure,
missing binary) affects EVERY job routed to that engine: without a breaker
the queue burns pending jobs one after another against a dead engine
(2026-07-02 incident: a codex usage limit permanently failed 14 requests in
minutes and spammed one Slack failure per request). trip() opens or extends
the breaker with a linearly escalating cooldown; open_until() lets workers
skip the engine while open; reset() closes it on the first successful run."""
from __future__ import annotations

import logging
from datetime import datetime

import psycopg

log = logging.getLogger("argus.breaker")

BASE_COOLDOWN_SECONDS = 900   # 15 min for the first trip
MAX_COOLDOWN_SECONDS = 3600   # cap: probe a long outage at least hourly


def trip(conn: psycopg.Connection, engine: str, reason: str) -> datetime:
    """Open (or extend) the breaker. Consecutive trips escalate linearly:
    15m, 30m, 45m, 60m, 60m, ... Returns the new open_until."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO engine_breaker (engine, open_until, reason)
            VALUES (%s, now() + make_interval(secs => %s), %s)
            ON CONFLICT (engine) DO UPDATE SET
                trip_count = engine_breaker.trip_count + 1,
                open_until = now() + make_interval(secs => LEAST(
                    %s * (engine_breaker.trip_count + 1), %s)),
                reason = EXCLUDED.reason,
                updated_at = now()
            RETURNING open_until, trip_count
            """,
            (engine, BASE_COOLDOWN_SECONDS, reason,
             BASE_COOLDOWN_SECONDS, MAX_COOLDOWN_SECONDS),
        )
        open_until_ts, trips = cur.fetchone()
    log.warning("engine breaker OPEN for %s until %s (trip %s): %s",
                engine, open_until_ts, trips, reason)
    return open_until_ts


def open_until(conn: psycopg.Connection, engine: str) -> datetime | None:
    """open_until when the breaker for engine is open, else None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT open_until FROM engine_breaker"
            " WHERE engine=%s AND open_until > now()",
            (engine,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def reset(conn: psycopg.Connection, engine: str) -> None:
    """Close the breaker after a successful run on this engine."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM engine_breaker WHERE engine=%s", (engine,))
        if cur.rowcount:
            log.info("engine breaker CLOSED for %s (successful run)", engine)
