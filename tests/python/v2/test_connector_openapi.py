"""Generic OpenAPI/HTTP connector: dotted-path extraction, fingerprinting,
high-water-mark cursor, OpenAPI operation resolution, and registration."""
from argus.v2.connectors.base import REGISTRY
from argus.v2.connectors.openapi import OpenAPIConnector


def test_registered_as_http():
    import argus.v2.connectors  # noqa: F401  (triggers registration)
    assert REGISTRY.get("http") is OpenAPIConnector


def test_extract_items_by_path_and_fingerprint():
    body = {"data": [{"id": "A", "title": "boom"}, {"id": "B", "title": "ok"}]}
    cfg = {"items_path": "data", "id_path": "id", "message_path": "title",
           "severity": "error"}
    signals, cursor = OpenAPIConnector.parse(body, {}, cfg=cfg, prefix="inc")
    assert [s.fingerprint for s in signals] == ["inc-A", "inc-B"]
    assert signals[0].payload["message"] == "boom"
    assert signals[0].payload["severity"] == "error"
    assert cursor == {}                      # no cursor_path => no high-water-mark


def test_whole_body_array_and_id_fallback_hash():
    body = [{"no_id": 1}, {"no_id": 2}]       # no id_path match => hashed, stable
    cfg = {"id_path": "id"}
    s1, _ = OpenAPIConnector.parse(body, {}, cfg=cfg, prefix="x")
    s2, _ = OpenAPIConnector.parse(body, {}, cfg=cfg, prefix="x")
    assert len(s1) == 2 and s1[0].fingerprint != s1[1].fingerprint
    assert [s.fingerprint for s in s1] == [s.fingerprint for s in s2]  # deterministic


def test_high_water_mark_cursor_suppresses_seen():
    cfg = {"items_path": "items", "id_path": "id", "cursor_path": "updated_at"}
    body = {"items": [{"id": "1", "updated_at": "2026-06-01"},
                      {"id": "2", "updated_at": "2026-06-02"}]}
    signals, cursor = OpenAPIConnector.parse(body, {}, cfg=cfg, prefix="t")
    assert len(signals) == 2 and cursor["cursor"] == "2026-06-02"

    # same body again => nothing new
    again, cursor2 = OpenAPIConnector.parse(body, cursor, cfg=cfg, prefix="t")
    assert again == [] and cursor2["cursor"] == "2026-06-02"

    # a newer item flows through
    body["items"].append({"id": "3", "updated_at": "2026-06-03"})
    fresh, cursor3 = OpenAPIConnector.parse(body, cursor2, cfg=cfg, prefix="t")
    assert [s.fingerprint for s in fresh] == ["t-3"] and cursor3["cursor"] == "2026-06-03"


def test_url_for_operation():
    spec = {"servers": [{"url": "https://api.x/v1/"}],
            "paths": {"/incidents": {"get": {"operationId": "listIncidents"}},
                      "/users": {"get": {"operationId": "listUsers"}}}}
    assert OpenAPIConnector.url_for_operation(spec, "listIncidents") == \
        "https://api.x/v1/incidents"
    assert OpenAPIConnector.url_for_operation(spec, "missing") == ""


def test_poll_wires_fetch_to_parse():
    cfg = {"items_path": "data", "id_path": "id", "fingerprint_prefix": "p"}
    source = type("S", (), {"config": cfg, "name": "src", "secret": None})()

    class Canned(OpenAPIConnector):
        def fetch(self, source, state):
            return {"data": [{"id": "Z"}]}

    signals, _ = Canned().poll(source, {})
    assert [s.fingerprint for s in signals] == ["p-Z"]
