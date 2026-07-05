"""Engine circuit breaker: trip opens with escalating cooldown, open_until
reports only a still-open breaker, reset closes. Without this, a queue full
of jobs burns one by one against an engine that is down for hours."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from argus.v2.queue import breaker


def test_trip_opens_breaker_with_base_cooldown(conn):
    until = breaker.trip(conn, "codex", "usage limit")
    conn.commit()
    now = datetime.now(timezone.utc)
    assert until > now + timedelta(seconds=breaker.BASE_COOLDOWN_SECONDS - 60)
    assert until <= now + timedelta(seconds=breaker.BASE_COOLDOWN_SECONDS + 60)
    assert breaker.open_until(conn, "codex") is not None


def test_consecutive_trips_escalate_and_cap(conn):
    first = breaker.trip(conn, "codex", "usage limit")
    second = breaker.trip(conn, "codex", "usage limit")
    conn.commit()
    assert second > first  # escalated: 2 * base > 1 * base
    for _ in range(10):
        capped = breaker.trip(conn, "codex", "usage limit")
    conn.commit()
    now = datetime.now(timezone.utc)
    assert capped <= now + timedelta(seconds=breaker.MAX_COOLDOWN_SECONDS + 60)


def test_open_until_none_when_closed_or_expired(conn):
    assert breaker.open_until(conn, "hermes") is None
    breaker.trip(conn, "hermes", "blip")
    conn.commit()
    with conn.cursor() as cur:  # simulate cooldown elapsed
        cur.execute("UPDATE engine_breaker SET open_until = now() - interval '1 second'"
                    " WHERE engine='hermes'")
    conn.commit()
    assert breaker.open_until(conn, "hermes") is None


def test_reset_closes_breaker(conn):
    breaker.trip(conn, "codex", "usage limit")
    conn.commit()
    breaker.reset(conn, "codex")
    conn.commit()
    assert breaker.open_until(conn, "codex") is None
    # And the next trip starts back at base cooldown (row was deleted).
    until = breaker.trip(conn, "codex", "again")
    conn.commit()
    now = datetime.now(timezone.utc)
    assert until <= now + timedelta(seconds=breaker.BASE_COOLDOWN_SECONDS + 60)


def test_breakers_are_per_engine(conn):
    breaker.trip(conn, "codex", "usage limit")
    conn.commit()
    assert breaker.open_until(conn, "codex") is not None
    assert breaker.open_until(conn, "claude-code") is None
