# tests/python/test_engine_router.py
import pytest

from argus.engine import EngineOutageError, EngineResult, UnknownEngineError
from argus.engine import router
from argus.engine.adapters import ADAPTERS


def _cfg(tmp_path, monkeypatch, body):
    f = tmp_path / "argus.config.yaml"
    f.write_text(body)
    monkeypatch.setenv("ARGUS_CONFIG", str(f))


def test_default_engine_precedence(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch, "engine:\n  default: codex\n")
    monkeypatch.setenv("ARGUS_ENGINE", "claude-code")
    # explicit arg > env > config > builtin echo
    assert router.default_engine("echo") == "echo"
    assert router.default_engine(None) == "claude-code"
    monkeypatch.delenv("ARGUS_ENGINE")
    assert router.default_engine(None) == "codex"
    monkeypatch.setenv("ARGUS_CONFIG", str(tmp_path / "absent.yaml"))
    assert router.default_engine(None) == "echo"


def test_fallback_engine_precedence(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch, "engine:\n  fallback: codex\n")
    monkeypatch.setenv("ARGUS_FALLBACK_ENGINE", "echo")
    assert router.fallback_engine("claude-code") == "claude-code"
    assert router.fallback_engine(None) == "echo"
    monkeypatch.delenv("ARGUS_FALLBACK_ENGINE")
    assert router.fallback_engine(None) == "codex"
    monkeypatch.setenv("ARGUS_CONFIG", str(tmp_path / "absent.yaml"))
    assert router.fallback_engine(None) is None


@pytest.fixture
def fake_adapters(monkeypatch):
    calls = []

    def outage(prompt):
        calls.append("outage")
        raise EngineOutageError("down")

    def ok(prompt):
        calls.append("ok")
        return EngineResult(text="OK")

    monkeypatch.setitem(ADAPTERS, "down-engine", outage)
    monkeypatch.setitem(ADAPTERS, "up-engine", ok)
    return calls


def test_fallback_runs_on_outage(fake_adapters, capsys):
    result = router.run_with_fallback("down-engine", "up-engine", "p")
    assert result.text == "OK"
    assert fake_adapters == ["outage", "ok"]
    assert "down-engine outage, trying fallback up-engine" in capsys.readouterr().err


def test_no_fallback_reraises(fake_adapters):
    with pytest.raises(EngineOutageError):
        router.run_with_fallback("down-engine", None, "p")
    with pytest.raises(EngineOutageError):
        router.run_with_fallback("down-engine", "down-engine", "p")


def test_both_down_reports_all_failed(fake_adapters, monkeypatch, capsys):
    def outage2(prompt):
        raise EngineOutageError("down too")

    monkeypatch.setitem(ADAPTERS, "down2", outage2)
    with pytest.raises(EngineOutageError):
        router.run_with_fallback("down-engine", "down2", "p")
    assert "all engines failed (down-engine, down2)" in capsys.readouterr().err


def test_unknown_engine_does_not_trigger_fallback(fake_adapters):
    # bash parity: rc 3 is not the outage code, so no failover.
    with pytest.raises(UnknownEngineError):
        router.run_with_fallback("no-such", "up-engine", "p")
    assert fake_adapters == []
