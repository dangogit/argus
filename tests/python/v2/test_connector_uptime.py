from argus.v2.connectors.uptime import UptimeConnector


def test_parse_down_emits_once_until_recovery():
    signals, cursor = UptimeConnector.parse("down", {}, project="luma", url="https://example.test")

    assert len(signals) == 1
    assert signals[0].fingerprint == "uptime-luma"
    assert signals[0].payload["severity"] == "error"
    assert signals[0].payload["url"] == "https://example.test"
    assert cursor == {"last_status": "down"}

    again, cursor = UptimeConnector.parse("down", cursor, project="luma", url="https://example.test")
    assert again == []
    assert cursor == {"last_status": "down"}


def test_parse_up_resets_down_cursor():
    down, cursor = UptimeConnector.parse("down", {}, project="luma")
    assert down

    up, cursor = UptimeConnector.parse("up", cursor, project="luma")
    assert up == []
    assert cursor == {"last_status": "up"}

    down_again, cursor = UptimeConnector.parse("down", cursor, project="luma")
    assert len(down_again) == 1
    assert cursor == {"last_status": "down"}


def test_poll_noops_without_url():
    signals, cursor = UptimeConnector().poll(
        type("Source", (), {"name": "uptime-luma", "team": "luma", "config": {}})(),
        {},
    )

    assert signals == []
    assert cursor == {"last_status": "up"}
