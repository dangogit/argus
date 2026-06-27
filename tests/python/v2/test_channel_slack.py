from types import SimpleNamespace

import pytest

from argus.v2.channels.slack import SlackChannel


def test_parse_app_mention_event():
    raw = {
        "type": "event_callback",
        "event_id": "Ev123",
        "event": {
            "type": "app_mention",
            "user": "U123",
            "channel": "C123",
            "ts": "1782370000.000100",
            "text": "<@B123> status please",
        },
    }

    msgs = SlackChannel().parse_inbound(raw)

    assert len(msgs) == 1
    msg = msgs[0]
    assert msg.chat_id == "C123"
    assert msg.text == "<@B123> status please"
    assert msg.dedup_key == "1782370000.000100"
    assert msg.sender_ref == "U123"
    assert msg.metadata == {
        "slack_event_type": "app_mention",
        "slack_ts": "1782370000.000100",
        "slack_thread_ts": "1782370000.000100",
    }


def test_parse_message_event_keeps_thread_metadata():
    raw = {
        "type": "event_callback",
        "event_id": "Ev124",
        "event": {
            "type": "message",
            "user": "U123",
            "channel": "C123",
            "ts": "1782370001.000100",
            "thread_ts": "1782370000.000100",
            "text": "send it",
        },
    }

    msgs = SlackChannel().parse_inbound(raw)

    assert len(msgs) == 1
    msg = msgs[0]
    assert msg.chat_id == "C123"
    assert msg.text == "send it"
    assert msg.dedup_key == "1782370001.000100"
    assert msg.sender_ref == "U123"
    assert msg.metadata == {
        "slack_event_type": "message",
        "slack_ts": "1782370001.000100",
        "slack_thread_ts": "1782370000.000100",
    }


def test_parse_message_event_prefers_slack_ts_for_dedup():
    raw = {
        "type": "event_callback",
        "event_id": "Ev124",
        "event": {
            "type": "message",
            "client_msg_id": "client-message-id",
            "user": "U123",
            "channel": "C123",
            "ts": "1782370001.000100",
            "text": "status please",
        },
    }

    msgs = SlackChannel().parse_inbound(raw)

    assert len(msgs) == 1
    assert msgs[0].dedup_key == "1782370001.000100"


def test_parse_skips_bot_message_subtype():
    raw = {
        "type": "event_callback",
        "event_id": "Ev123",
        "event": {
            "type": "message",
            "subtype": "bot_message",
            "channel": "C123",
            "ts": "1782370000.000100",
            "text": "ignore me",
        },
    }

    assert SlackChannel().parse_inbound(raw) == []


def test_parse_skips_bot_message_without_subtype():
    raw = {
        "type": "event_callback",
        "event_id": "Ev123",
        "event": {
            "type": "message",
            "bot_id": "B123",
            "channel": "C123",
            "ts": "1782370000.000100",
            "text": "ignore me",
        },
    }

    assert SlackChannel().parse_inbound(raw) == []


def test_send_posts_chat_message(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True, "ts": "1782370000.000100"}

    def post(url, *, headers, json, timeout):
        calls.append((url, headers, json, timeout))
        return Response()

    import httpx

    monkeypatch.setattr(httpx, "post", post)
    binding = SimpleNamespace(channel_id="C123", secret="xoxb-token")

    ref = SlackChannel().send(binding, "all done")

    assert ref == "1782370000.000100"
    assert calls == [(
        "https://slack.com/api/chat.postMessage",
        {"Authorization": "Bearer xoxb-token"},
        {"channel": "C123", "text": "all done"},
        20,
    )]


def test_send_raises_slack_api_error(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": False, "error": "channel_not_found"}

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: Response())
    binding = SimpleNamespace(channel_id="C123", secret="xoxb-token")

    with pytest.raises(RuntimeError, match="channel_not_found"):
        SlackChannel().send(binding, "all done")
