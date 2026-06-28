"""Connector failure backoff: a failing source records its error, backs off,
and is skipped until the window passes; a recovery clears the state."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from argus.v2.config import loader
from argus.v2.connectors import driver

_PAST = datetime(2000, 1, 1, tzinfo=timezone.utc)
_FUTURE = datetime(2999, 1, 1, tzinfo=timezone.utc)


def _cfg(tmp_path):
    y = tmp_path / "a.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "  sources:\n"
        f"    - {{ name: src1, type: fake, scope: company, team: dev, config: {json.dumps({'signals': []})} }}\n"
        "teams:\n  - name: dev\n    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n")
    return loader.load(y)


class _Boom:
    def poll(self, source, state):
        raise RuntimeError("provider exploded")


class _Ok:
    def poll(self, source, state):
        return [], {"idx": 1}


def test_backoff_seconds_grows_and_caps():
    assert driver._backoff_seconds(0) == 0.0
    assert driver._backoff_seconds(1) == 30.0
    assert driver._backoff_seconds(2) == 60.0
    assert driver._backoff_seconds(3) == 120.0
    assert driver._backoff_seconds(99) == driver._BACKOFF_MAX


def _state(conn, name="src1"):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT error_count, last_error, poll_after FROM connector_state WHERE source_name=%s",
            (name,))
        return cur.fetchone()


def test_failure_records_error_and_schedules_backoff(conn, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setitem(driver.REGISTRY, "fake", _Boom)
    assert driver.poll_once(conn, cfg) == 0
    error_count, last_error, poll_after = _state(conn)
    assert error_count == 1
    assert "RuntimeError" in last_error
    assert poll_after is not None


def test_consecutive_failures_increment(conn, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setitem(driver.REGISTRY, "fake", _Boom)
    # Each call must be allowed to actually poll, so pass now in the future to
    # bypass the just-set backoff window, proving the counter climbs.
    driver.poll_once(conn, cfg, now=_FUTURE)
    driver.poll_once(conn, cfg, now=_FUTURE)
    error_count, _, _ = _state(conn)
    assert error_count == 2


def test_source_in_backoff_is_skipped(conn, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setitem(driver.REGISTRY, "fake", _Boom)
    driver.poll_once(conn, cfg)  # fail once -> poll_after in the (real) future
    # now is before poll_after: the source is skipped, counter unchanged.
    driver.poll_once(conn, cfg, now=_PAST)
    error_count, _, _ = _state(conn)
    assert error_count == 1


def test_recovery_clears_backoff(conn, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setitem(driver.REGISTRY, "fake", _Boom)
    driver.poll_once(conn, cfg)  # error_count -> 1
    monkeypatch.setitem(driver.REGISTRY, "fake", _Ok)
    driver.poll_once(conn, cfg, now=_FUTURE)  # past the window, succeeds
    error_count, last_error, poll_after = _state(conn)
    assert error_count == 0
    assert last_error is None
    assert poll_after is None
