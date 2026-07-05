"""Connector failure backoff: a failing source records its error, backs off,
and is skipped until the window passes; a recovery clears the state."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx

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


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.example.test")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


class _Unauthorized:
    def poll(self, source, state):
        raise _http_error(401)


class _ServerError:
    def poll(self, source, state):
        raise _http_error(504)


def _category(conn, name="src1") -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT error_category FROM connector_state WHERE source_name=%s", (name,))
        row = cur.fetchone()
    return row[0] if row else None


def test_auth_failure_jumps_straight_to_max_backoff(conn, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setitem(driver.REGISTRY, "fake", _Unauthorized)
    driver.poll_once(conn, cfg)
    error_count, last_error, poll_after = _state(conn)
    assert error_count == 1
    assert "AUTH" in last_error
    assert _category(conn) == "auth"
    with conn.cursor() as cur:
        cur.execute("SELECT last_error_at FROM connector_state WHERE source_name='src1'")
        last_error_at = cur.fetchone()[0]
    delay = (poll_after - last_error_at).total_seconds()
    assert delay == driver._BACKOFF_MAX


def test_transient_5xx_failure_uses_normal_ladder(conn, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setitem(driver.REGISTRY, "fake", _ServerError)
    driver.poll_once(conn, cfg)
    error_count, last_error, poll_after = _state(conn)
    assert error_count == 1
    assert "AUTH" not in last_error
    assert _category(conn) == "transient"
    with conn.cursor() as cur:
        cur.execute("SELECT last_error_at FROM connector_state WHERE source_name='src1'")
        last_error_at = cur.fetchone()[0]
    delay = (poll_after - last_error_at).total_seconds()
    assert delay == driver._BACKOFF_BASE  # first failure: normal ladder, not maxed out


def test_401_and_504_get_distinct_labels_and_backoff(conn, tmp_path, monkeypatch):
    """A 401 (permanent, needs a human) must be treated differently from a 504
    (transient, worth the normal retry ladder): distinct error label, and the
    401 jumps to max backoff instead of climbing from the base delay."""
    cfg = _cfg(tmp_path)

    monkeypatch.setitem(driver.REGISTRY, "fake", _Unauthorized)
    driver.poll_once(conn, cfg)
    auth_count, auth_label, auth_poll_after = _state(conn)
    assert _category(conn) == "auth"

    with conn.cursor() as cur:
        cur.execute("UPDATE connector_state SET error_count=0, poll_after=NULL WHERE source_name='src1'")
    conn.commit()

    monkeypatch.setitem(driver.REGISTRY, "fake", _ServerError)
    driver.poll_once(conn, cfg)
    transient_count, transient_label, transient_poll_after = _state(conn)
    assert _category(conn) == "transient"

    assert auth_label != transient_label
    assert "AUTH" in auth_label and "AUTH" not in transient_label
