import pytest

from argus.engine import run_agent, UnknownEngineError


def test_echo_round_trip():
    result = run_agent("echo", "hello world")
    assert result.text == "ECHO: hello world"
    assert result.cost_source == "unpriced"


def test_unknown_engine_raises():
    with pytest.raises(UnknownEngineError):
        run_agent("no-such-engine", "x")


def test_run_agent_resets_meta_sink(tmp_path, monkeypatch):
    sink = tmp_path / "meta"
    sink.write_text("stale")
    monkeypatch.setenv("ARGUS_ENGINE_META", str(sink))
    run_agent("echo", "hi")
    assert "stale" not in sink.read_text()
    assert "costSource=unpriced" in sink.read_text()
