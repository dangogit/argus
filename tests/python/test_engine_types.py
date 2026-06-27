import pytest

from argus.engine import EngineResult, EngineOutageError, UnknownEngineError, OUTAGE_RC, UNKNOWN_ENGINE_RC, write_meta


def test_constants():
    assert OUTAGE_RC == 42
    assert UNKNOWN_ENGINE_RC == 3


def test_result_validates_cost_source():
    EngineResult(text="x", cost_source="exact")
    EngineResult(text="x", cost_source="estimated")
    EngineResult(text="x", cost_source="unpriced")
    with pytest.raises(ValueError):
        EngineResult(text="x", cost_source="made-up")


def test_write_meta_honors_env_sink(tmp_path, monkeypatch):
    sink = tmp_path / "meta"
    monkeypatch.setenv("ARGUS_ENGINE_META", str(sink))
    write_meta("estimated", "0.12")
    assert sink.read_text() == "costSource=estimated\ncostUsd=0.12\n"


def test_write_meta_noop_without_env(monkeypatch):
    monkeypatch.delenv("ARGUS_ENGINE_META", raising=False)
    write_meta("unpriced", "")  # must not raise


def test_write_meta_rejects_invalid_cost_source():
    with pytest.raises(ValueError):
        write_meta("made-up", "0.1")
