import json
from pathlib import Path

from argus.v2.connectors.sentry import SentryConnector

RAW = json.loads((Path(__file__).parent / "fixtures" / "connectors" / "sentry_issues.json").read_text())


def test_parse_keeps_error_and_above_only():
    signals, cursor = SentryConnector.parse(RAW, {}, min_level="error")
    fps = {s.fingerprint for s in signals}
    assert fps == {"1001", "1003"}  # error + fatal; the warning is dropped


def test_parse_advances_cursor_and_filters_old():
    _, cursor = SentryConnector.parse(RAW, {}, min_level="error")
    assert cursor["last_seen"] == "2026-06-15T12:00:00Z"
    # A second parse with that cursor yields nothing (all <= last_seen).
    signals2, _ = SentryConnector.parse(RAW, cursor, min_level="error")
    assert signals2 == []


def test_parse_signal_payload_has_title_and_link():
    signals, _ = SentryConnector.parse(RAW, {}, min_level="fatal")
    s = signals[0]
    assert s.payload["title"] == "OOM killed" and "permalink" in s.payload
