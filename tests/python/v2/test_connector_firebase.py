import json
import time
from pathlib import Path
from types import SimpleNamespace

from argus.v2.connectors import driver
from argus.v2.connectors.firebase import FirebaseConnector, _auth_token
from argus.v2.config import loader

RAW = json.loads(
    (Path(__file__).parent / "fixtures" / "connectors" / "firebase_documents.json").read_text()
)


def test_parse_unwraps_firestore_fields_and_maps_severity():
    signals, cursor = FirebaseConnector.parse(
        RAW,
        {},
        project="luma",
        collection="bug_reports",
        severity_column="severity",
    )

    assert {s.fingerprint for s in signals} == {
        "firebase-luma-bug_reports-abc",
        "firebase-luma-bug_reports-def",
    }
    assert signals[0].payload["message"] == "Push fails on iOS"
    assert signals[0].payload["severity"] == "error"
    assert signals[0].payload["fields"]["count"] == "3"
    assert signals[0].payload["fields"]["blocked"] is True
    assert signals[1].payload["message"] == "Checkout copy is unclear"
    assert signals[1].payload["severity"] == "info"
    assert cursor["seen"] == [
        "firebase-luma-bug_reports-abc",
        "firebase-luma-bug_reports-def",
    ]


def test_parse_seen_cursor_dedups_open_docs():
    _, cursor = FirebaseConnector.parse(
        RAW,
        {},
        project="luma",
        collection="bug_reports",
        severity_column="severity",
    )
    again, new_cursor = FirebaseConnector.parse(
        RAW,
        cursor,
        project="luma",
        collection="bug_reports",
        severity_column="severity",
    )

    assert again == []
    assert new_cursor == cursor


def test_auth_token_prefers_explicit_secret(tmp_path):
    auth_file = tmp_path / "missing.json"
    source = SimpleNamespace(secret="pat", config={"auth_file": str(auth_file)})

    assert _auth_token(source) == "pat"


def test_auth_token_reads_fresh_firebase_cli_auth_file(tmp_path):
    auth_file = tmp_path / "firebase-tools.json"
    auth_file.write_text(json.dumps({
        "tokens": {
            "access_token": "cli-token",
            "expires_at": int(time.time() * 1000) + 3600_000,
        },
    }), encoding="utf-8")
    source = SimpleNamespace(secret=None, config={"auth_file": str(auth_file)})

    assert _auth_token(source) == "cli-token"


def test_auth_token_refreshes_expired_firebase_cli_auth_file(tmp_path, monkeypatch):
    auth_file = tmp_path / "firebase-tools.json"
    auth_file.write_text(json.dumps({
        "tokens": {
            "access_token": "old-token",
            "refresh_token": "refresh-token",
            "expires_at": int(time.time() * 1000) - 1,
        },
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
    assert calls == [("https://www.googleapis.com/oauth2/v3/token", "refresh_token")]
    assert json.loads(auth_file.read_text(encoding="utf-8"))["tokens"]["access_token"] == "new-token"


def test_driver_ingests_firebase_signals(conn, tmp_path, monkeypatch):
    class FakeFirebase(FirebaseConnector):
        def fetch(self, source, state):
            return RAW

    monkeypatch.setitem(driver.REGISTRY, "firebase", FakeFirebase)
    cfg_file = tmp_path / "argus.yaml"
    cfg_file.write_text(
        "company:\n"
        "  name: c\n"
        "  defaults: { engine: { engine: echo } }\n"
        "  sources:\n"
        "    - name: firebase-luma\n"
        "      type: firebase\n"
        "      scope: company\n"
        "      team: luma\n"
        "      config: { project: luma, collection: bug_reports, severity_column: severity }\n"
        "teams:\n"
        "  - name: luma\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
    )
    cfg = loader.load(cfg_file)

    assert driver.poll_once(conn, cfg) == 2
    assert driver.poll_once(conn, cfg) == 0
