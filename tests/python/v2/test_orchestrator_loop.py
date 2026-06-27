"""Orchestrator resilience: a crashing sweep must not take the loop down.
Hermetic - no DB; pool.connect and select.select are stubbed."""
from __future__ import annotations

import pytest

from argus.v2.orchestrator import loop


class _FakeCursor:
    def __init__(self):
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        # The advisory-lock acquire is the only fetched value the loop reads.
        if "pg_try_advisory_lock" in sql:
            self._row = (True,)

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self):
        self.autocommit = False
        self.committed = 0
        self.rolledback = 0
        self.closed = 0

    def cursor(self):
        return _FakeCursor()

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolledback += 1

    def close(self):
        self.closed += 1

    def notifies(self):
        return []


def test_backoff_seconds_escalates_and_caps():
    assert loop._backoff_seconds(0) == 0.0
    assert loop._backoff_seconds(1) == 1.0
    assert loop._backoff_seconds(2) == 2.0
    # Past the table length it caps at the last value, never grows unbounded.
    assert loop._backoff_seconds(99) == loop._BACKOFF[-1]


def test_sweep_swallows_exception(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(loop.pool, "connect", lambda: conn)
    monkeypatch.setattr(loop.reconcile, "sweep_once",
                        lambda c, cfg: (_ for _ in ()).throw(RuntimeError("boom")))
    # Must not raise; must roll back and close its connection.
    assert loop._sweep(cfg=object()) is False
    assert conn.rolledback == 1
    assert conn.closed == 1


def test_sweep_success(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(loop.pool, "connect", lambda: conn)
    monkeypatch.setattr(loop.reconcile, "sweep_once", lambda c, cfg: None)
    assert loop._sweep(cfg=object()) is True
    assert conn.committed == 1
    assert conn.closed == 1


def _patch_loop(monkeypatch, sweep_results):
    """Drive run() with a scripted sequence of sweep outcomes. Captures the
    timeout passed to select.select each iteration so we can assert backoff."""
    timeouts = []
    monkeypatch.setattr(loop.pool, "connect", lambda: _FakeConn())
    monkeypatch.setattr(loop.select, "select",
                        lambda r, w, x, timeout: timeouts.append(timeout) or ([], [], []))
    calls = {"n": 0}

    def fake_sweep(cfg):
        i = calls["n"]
        calls["n"] += 1
        ok = sweep_results[i]
        if not ok:
            raise RuntimeError("sweep boom")
        return None

    monkeypatch.setattr(loop.reconcile, "sweep_once", lambda c, cfg: fake_sweep(cfg))
    return timeouts


def test_run_survives_consecutive_sweep_crashes(monkeypatch):
    # Every sweep crashes; the loop must keep going and back off 1 -> 2 -> 5.
    timeouts = _patch_loop(monkeypatch, [False, False, False])
    loop.run(cfg=object(), poll_seconds=0.0, max_iterations=3)
    assert timeouts == [1.0, 2.0, 5.0]


def test_run_resets_backoff_on_success(monkeypatch):
    # fail, succeed, fail -> backoff resets to 0 after the good sweep.
    timeouts = _patch_loop(monkeypatch, [False, True, False])
    loop.run(cfg=object(), poll_seconds=0.0, max_iterations=3)
    assert timeouts == [1.0, 0.0, 1.0]
