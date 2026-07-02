"""Shared connector HTTP helper: fetch_json's timeout/raise_for_status
convention and the classify() failure bucketer the driver relies on to tell a
permanent auth failure apart from a transient one."""
import httpx
import pytest

from argus.v2.connectors.client import classify, fetch_json


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.example.test")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def test_classify_401_and_403_as_auth():
    assert classify(_status_error(401)) == "auth"
    assert classify(_status_error(403)) == "auth"


def test_classify_429_as_rate_limit():
    assert classify(_status_error(429)) == "rate_limit"


def test_classify_5xx_as_transient():
    assert classify(_status_error(500)) == "transient"
    assert classify(_status_error(503)) == "transient"


def test_classify_other_4xx_as_other():
    assert classify(_status_error(404)) == "other"
    assert classify(_status_error(400)) == "other"


def test_classify_timeout_and_network_error_as_transient():
    request = httpx.Request("GET", "https://api.example.test")
    assert classify(httpx.ConnectTimeout("timed out", request=request)) == "transient"
    assert classify(httpx.ConnectError("refused", request=request)) == "transient"


def test_classify_unknown_exception_as_other():
    assert classify(ValueError("not an http error")) == "other"


def test_fetch_json_returns_parsed_body_and_raises_on_error(monkeypatch):
    calls = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["url"] = url
        calls["headers"] = headers
        calls["params"] = params
        calls["timeout"] = timeout
        request = httpx.Request("GET", url)
        return httpx.Response(200, json={"ok": True}, request=request)

    monkeypatch.setattr(httpx, "get", fake_get)
    result = fetch_json("https://api.example.test/x", headers={"A": "b"},
                        params={"q": "1"}, timeout=5)

    assert result == {"ok": True}
    assert calls == {"url": "https://api.example.test/x", "headers": {"A": "b"},
                      "params": {"q": "1"}, "timeout": 5}


def test_fetch_json_uses_default_timeout(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["timeout"] = timeout
        request = httpx.Request("GET", url)
        return httpx.Response(200, json={}, request=request)

    monkeypatch.setattr(httpx, "get", fake_get)
    fetch_json("https://api.example.test/x")

    assert captured["timeout"] == 20.0


def test_fetch_json_raises_http_status_error_on_failure(monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=None):
        request = httpx.Request("GET", url)
        return httpx.Response(401, json={"error": "nope"}, request=request)

    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(httpx.HTTPStatusError):
        fetch_json("https://api.example.test/x")
