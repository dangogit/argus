import json
import time
from pathlib import Path
from types import SimpleNamespace

from argus.v2.connectors.vercel import VercelConnector, VercelEventsConnector, _auth_token

RAW = json.loads((Path(__file__).parent / "fixtures" / "connectors" / "vercel_deployments.json").read_text())
EVENTS = json.loads((Path(__file__).parent / "fixtures" / "connectors" / "vercel_events.json").read_text())


def test_parse_keeps_failed_deployments_only():
    signals, cursor = VercelConnector.parse(RAW, {})
    assert {s.fingerprint for s in signals} == {"dpl_2", "dpl_3"}  # READY dropped
    assert cursor["last_created"] == 3000


def test_parse_cursor_filters_seen():
    _, cursor = VercelConnector.parse(RAW, {})
    again, _ = VercelConnector.parse(RAW, cursor)
    assert again == []


def test_parse_signal_payload():
    signals, _ = VercelConnector.parse(RAW, {})
    s = next(x for x in signals if x.fingerprint == "dpl_2")
    assert s.payload["state"] == "ERROR" and s.payload["name"] == "sample-app"


def test_auth_token_prefers_explicit_secret(tmp_path):
    auth_file = tmp_path / "missing.json"
    source = SimpleNamespace(secret="pat", config={"auth_file": str(auth_file)})

    assert _auth_token(source) == "pat"


def test_auth_token_reads_fresh_vercel_cli_auth_file(tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({
        "token": "cli-token",
        "expiresAt": int(time.time()) + 3600,
    }), encoding="utf-8")
    source = SimpleNamespace(secret=None, config={"auth_file": str(auth_file)})

    assert _auth_token(source) == "cli-token"


def test_auth_token_refreshes_expired_vercel_cli_auth_file(tmp_path, monkeypatch):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({
        "token": "old-token",
        "refreshToken": "refresh-token",
        "expiresAt": int(time.time()) - 1,
    }), encoding="utf-8")

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"access_token": "new-token", "expires_in": 3600}

    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs["data"]["grant_type"]))
        return Response()

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)
    source = SimpleNamespace(secret=None, config={"auth_file": str(auth_file)})

    assert _auth_token(source) == "new-token"
    assert calls == [("https://api.vercel.com/login/oauth/token", "refresh_token")]
    assert json.loads(auth_file.read_text(encoding="utf-8"))["token"] == "new-token"


def test_events_parse_keeps_5xx_events_only():
    signals, cursor = VercelEventsConnector.parse(
        {"events": EVENTS, "_argus_since_ms": 1780000000000},
        {},
        deployment="dpl_live",
    )

    assert {s.fingerprint for s in signals} == {
        "vercel-event-dpl_live-evt_500",
        "vercel-event-dpl_live-evt_502",
    }
    assert cursor["last_created"] == 1780000003000


def test_events_parse_cursor_filters_seen():
    _, cursor = VercelEventsConnector.parse(
        {"events": EVENTS, "_argus_since_ms": 1780000000000},
        {},
        deployment="dpl_live",
    )
    again, _ = VercelEventsConnector.parse(
        {"events": EVENTS, "_argus_since_ms": 1780000000000},
        cursor,
        deployment="dpl_live",
    )

    assert again == []


def test_events_signal_payload_is_owner_safe():
    signals, _ = VercelEventsConnector.parse(
        {"events": EVENTS, "_argus_since_ms": 1780000000000},
        {},
        deployment="dpl_live",
    )
    signal = next(s for s in signals if s.payload["status_code"] == 502)

    assert signal.payload["message"] == "Vercel 502 response on /api/webhook"
    assert signal.payload["url"] == "https://example.vercel.app/api/webhook"
    assert "clientIp" not in signal.payload


def test_events_poll_resolves_latest_deployment(monkeypatch):
    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_get(url, **kwargs):
        calls.append((url, kwargs["params"]))
        if url.endswith("/v6/deployments"):
            return Response({"deployments": [{"uid": "dpl_live"}]})
        if url.endswith("/v3/deployments/dpl_live/events"):
            return Response(EVENTS)
        raise AssertionError(url)

    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)
    source = SimpleNamespace(
        secret="token",
        config={
            "project": "prj_123",
            "team": "team_123",
            "lookback_seconds": 10_000_000,
        },
    )

    signals, cursor = VercelEventsConnector().poll(source, {})

    assert len(signals) == 2
    assert cursor["last_created"] == 1780000003000
    assert calls[0][1]["target"] == "production"
    assert calls[0][1]["state"] == "READY"
    assert calls[1][1]["statusCode"] == "5xx"
    assert calls[1][1]["teamId"] == "team_123"
