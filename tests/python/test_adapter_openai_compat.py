"""OpenAI-compatible adapter (openrouter / ollama). Hermetic: urlopen stubbed."""
from __future__ import annotations

import json
import urllib.error

import pytest

from argus.engine import EngineOutageError, run_agent
from argus.engine.adapters import ADAPTERS, openai_compat as oc


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _stub(monkeypatch, payload, captured=None):
    def fake_urlopen(req, timeout=None):
        if captured is not None:
            captured.append(req)
        return _Resp(payload)
    monkeypatch.setattr(oc.urllib.request, "urlopen", fake_urlopen)


def test_registry_has_new_engines():
    assert "openrouter" in ADAPTERS and "ollama" in ADAPTERS


def test_openrouter_happy_path(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k-123")
    monkeypatch.setenv("ARGUS_MODEL", "anthropic/claude-3.5")
    captured = []
    _stub(monkeypatch, {"choices": [{"message": {"content": "hello there\n"}}]}, captured)
    result = run_agent("openrouter", "hi")
    assert result.text == "hello there"
    req = captured[0]
    assert req.full_url == "https://openrouter.ai/api/v1/chat/completions"
    assert req.headers["Authorization"] == "Bearer k-123"
    assert json.loads(req.data)["model"] == "anthropic/claude-3.5"


def test_openrouter_missing_key_is_outage(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(EngineOutageError):
        oc.openrouter("hi")


def test_ollama_no_auth_default_model(monkeypatch):
    monkeypatch.delenv("ARGUS_MODEL", raising=False)
    captured = []
    _stub(monkeypatch, {"choices": [{"message": {"content": "yo"}}]}, captured)
    result = oc.ollama("hi")
    assert result.text == "yo"
    req = captured[0]
    assert "Authorization" not in req.headers
    assert req.full_url.startswith("http://localhost:11434/v1")
    assert json.loads(req.data)["model"] == "llama3.1"


def test_url_error_is_outage(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")

    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(oc.urllib.request, "urlopen", boom)
    with pytest.raises(EngineOutageError):
        oc.openrouter("hi")


def test_bad_response_shape_is_outage(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    _stub(monkeypatch, {"unexpected": True})
    with pytest.raises(EngineOutageError):
        oc.openrouter("hi")
