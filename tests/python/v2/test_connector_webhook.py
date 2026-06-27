from types import SimpleNamespace

from argus.v2.connectors.webhook import WebhookConnector


RAW = {
    "events": [
        {"id": "a", "timestamp": "2026-06-18T10:00:00Z", "message": "A"},
        {"fingerprint": "b", "timestamp": "2026-06-18T11:00:00Z", "message": "B"},
    ],
}


def test_webhook_normalizes_events_and_tracks_timestamp():
    signals, cursor = WebhookConnector.parse(RAW, {}, source_name="hook")

    assert [signal.fingerprint for signal in signals] == ["a", "b"]
    assert signals[0].payload["source"] == "webhook"
    assert cursor == {"last_seen": "2026-06-18T11:00:00Z"}

    again, cursor2 = WebhookConnector.parse(RAW, cursor, source_name="hook")

    assert again == []
    assert cursor2 == cursor


def test_webhook_hashes_events_without_fingerprint():
    signals, _ = WebhookConnector.parse({"message": "no id"}, {}, source_name="hook")

    assert signals[0].fingerprint.startswith("webhook-hook-")
    assert signals[0].payload["kind"] == "incident"


def test_webhook_poll_can_use_static_config_events():
    source = SimpleNamespace(name="hook", config={"events": [{"id": "x"}]}, secret=None)

    signals, _ = WebhookConnector().poll(source, {})

    assert [signal.fingerprint for signal in signals] == ["x"]
