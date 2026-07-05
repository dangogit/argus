import os
from dataclasses import dataclass

import pytest

from argus.engine import EngineOutageError
from argus.v2 import engine_runner


@dataclass
class Result:
    text: str


def test_run_with_fallback_returns_primary_on_success():
    calls = []

    def run_agent(engine, prompt):
        calls.append(engine)
        return Result(f"{engine}:{prompt}")

    out = engine_runner.run_with_fallback(run_agent, "primary", "backup", "p")

    assert out == "primary:p"
    assert calls == ["primary"]


def test_run_with_fallback_fires_on_outage():
    calls = []

    def run_agent(engine, prompt):
        calls.append(engine)
        if engine == "primary":
            raise EngineOutageError("down")
        return Result("rescued")

    out = engine_runner.run_with_fallback(run_agent, "primary", "backup", "p")

    assert out == "rescued"
    assert calls == ["primary", "backup"]


def test_run_with_fallback_reraises_without_fallback():
    def run_agent(engine, prompt):
        raise EngineOutageError("down")

    with pytest.raises(EngineOutageError):
        engine_runner.run_with_fallback(run_agent, "primary", None, "p")
    with pytest.raises(EngineOutageError):
        engine_runner.run_with_fallback(run_agent, "primary", "", "p")
    with pytest.raises(EngineOutageError):
        engine_runner.run_with_fallback(run_agent, "primary", "primary", "p")


def test_run_with_fallback_only_catches_outages():
    calls = []

    def run_agent(engine, prompt):
        calls.append(engine)
        raise ValueError("real agent error")

    with pytest.raises(ValueError):
        engine_runner.run_with_fallback(run_agent, "primary", "backup", "p")
    assert calls == ["primary"]


def test_tool_less_env_applies_and_restores(monkeypatch):
    monkeypatch.setenv("ARGUS_CLAUDE_TOOLS", "Bash,Read")
    monkeypatch.delenv("ARGUS_CODEX_SANDBOX", raising=False)
    monkeypatch.delenv("ARGUS_AGENT_CWD", raising=False)

    with engine_runner.tool_less_env(prefix="argus-test-"):
        assert os.environ["ARGUS_CLAUDE_TOOLS"] == ""
        assert os.environ["ARGUS_CODEX_SANDBOX"] == "read-only"
        cwd = os.environ["ARGUS_AGENT_CWD"]
        assert "argus-test-" in cwd
        assert os.path.isdir(cwd)

    assert os.environ["ARGUS_CLAUDE_TOOLS"] == "Bash,Read"
    assert "ARGUS_CODEX_SANDBOX" not in os.environ
    assert "ARGUS_AGENT_CWD" not in os.environ
    assert not os.path.isdir(cwd)


def test_tool_less_env_restores_after_exception(monkeypatch):
    monkeypatch.setenv("ARGUS_CODEX_SANDBOX", "danger-full-access")

    with pytest.raises(RuntimeError):
        with engine_runner.tool_less_env(prefix="argus-test-"):
            raise RuntimeError("boom")

    assert os.environ["ARGUS_CODEX_SANDBOX"] == "danger-full-access"


def test_tool_less_env_timeout_applied_and_restored(monkeypatch):
    monkeypatch.delenv("ARGUS_ENGINE_TIMEOUT", raising=False)

    with engine_runner.tool_less_env(prefix="argus-test-", timeout="90"):
        assert os.environ["ARGUS_ENGINE_TIMEOUT"] == "90"

    assert "ARGUS_ENGINE_TIMEOUT" not in os.environ


def test_tool_less_env_timeout_defers_to_outer_value(monkeypatch):
    monkeypatch.setenv("ARGUS_ENGINE_TIMEOUT", "15")

    with engine_runner.tool_less_env(prefix="argus-test-", timeout="90"):
        assert os.environ["ARGUS_ENGINE_TIMEOUT"] == "15"

    assert os.environ["ARGUS_ENGINE_TIMEOUT"] == "15"


def test_tool_less_env_without_timeout_leaves_timeout_alone(monkeypatch):
    monkeypatch.setenv("ARGUS_ENGINE_TIMEOUT", "15")

    with engine_runner.tool_less_env(prefix="argus-test-"):
        assert os.environ["ARGUS_ENGINE_TIMEOUT"] == "15"

    assert os.environ["ARGUS_ENGINE_TIMEOUT"] == "15"
