import json
from pathlib import Path

from argus.v2.connectors.posthog import PostHogConnector

RAW = json.loads((Path(__file__).parent / "fixtures" / "connectors" / "posthog_activity.json").read_text())
ISSUES = json.loads((Path(__file__).parent / "fixtures" / "connectors" / "posthog_issues.json").read_text())


def test_parse_keeps_alert_fired_only():
    signals, cursor = PostHogConnector.parse(RAW, {})
    assert {s.fingerprint for s in signals} == {"9001", "9003"}
    assert signals[0].payload["name"] == "Signups dropped"


def test_parse_cursor_filters_seen():
    _, cursor = PostHogConnector.parse(RAW, {})
    assert cursor["last_created"] == "2026-06-15T10:00:00Z"
    again, _ = PostHogConnector.parse(RAW, cursor)
    assert again == []


def test_parse_error_tracking_issues_normalizes_active_only():
    signals, cursor = PostHogConnector.parse_issues(
        ISSUES,
        {},
        project="luma",
        host="https://us.posthog.com",
        project_id="42",
    )

    assert {s.fingerprint for s in signals} == {"posthog-luma-iss_1", "posthog-luma-iss_3"}
    assert signals[0].payload["message"] == "Upload failed"
    assert signals[0].payload["count"] == 12
    assert signals[0].payload["url"] == "https://us.posthog.com/project/42/error_tracking/iss_1"
    assert signals[1].payload["message"] == "Checkout timeout"
    assert cursor["seen"] == ["posthog-luma-iss_1", "posthog-luma-iss_3"]


def test_parse_error_tracking_issues_seen_cursor_dedups():
    _, cursor = PostHogConnector.parse_issues(
        ISSUES,
        {},
        project="luma",
    )
    again, new_cursor = PostHogConnector.parse_issues(
        ISSUES,
        cursor,
        project="luma",
    )

    assert again == []
    assert new_cursor == cursor
