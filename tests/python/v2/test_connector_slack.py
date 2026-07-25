"""Slack feedback-channel connector tests: filtering matrix, cursor advance,
pagination, 429 partial-fetch behavior, reply_to origin, driver integration."""
import httpx
import pytest

from argus.v2.connectors import driver
from argus.v2.connectors.slack import SlackConnector
from argus.v2.config import loader
from argus.v2.config.schema import SourceRef


def _msg(ts, text="hello", user="U1", **extra):
    return {"type": "message", "ts": ts, "text": text, "user": user, **extra}


# --- parse: mapping ----------------------------------------------------------

def test_parse_maps_message_to_signal_with_reply_to():
    raw = [_msg("1710000000.000100", text="checkout is broken", user="U42")]
    signals, state = SlackConnector.parse(raw, {}, channel="C1")
    assert len(signals) == 1
    sig = signals[0]
    assert sig.fingerprint == "slack-C1-1710000000.000100"
    assert sig.payload["source"] == "slack"
    assert sig.payload["message"] == "checkout is broken"
    assert sig.payload["kind"] == "feedback"
    assert sig.payload["severity"] == "info"
    assert sig.payload["channel"] == "C1"
    assert sig.payload["ts"] == "1710000000.000100"
    assert sig.payload["user"] == "U42"
    assert sig.reply_to == {"kind": "slack_thread", "channel": "C1",
                            "ts": "1710000000.000100"}
    assert state["last_ts"] == "1710000000.000100"


def test_parse_emits_signals_oldest_first():
    # conversations.history returns newest-first; signals ingest oldest-first.
    raw = [_msg("1710000003.000000", text="third"),
           _msg("1710000002.000000", text="second"),
           _msg("1710000001.000000", text="first")]
    signals, state = SlackConnector.parse(raw, {}, channel="C1")
    assert [s.payload["message"] for s in signals] == ["first", "second", "third"]
    assert state["last_ts"] == "1710000003.000000"


def test_parse_permalink_built_from_workspace_config():
    raw = [_msg("1710000000.000100")]
    signals, _ = SlackConnector.parse(raw, {}, channel="C1",
                                      workspace="acme")
    assert signals[0].payload["permalink"] == \
        "https://acme.slack.com/archives/C1/p1710000000000100"
    signals2, _ = SlackConnector.parse(raw, {}, channel="C1")
    assert signals2[0].payload["permalink"] == ""


# --- parse: filtering matrix -------------------------------------------------

def test_parse_skips_bot_messages():
    raw = [_msg("1710000001.000000", bot_id="B99"),
           {"type": "message", "ts": "1710000002.000000",
            "subtype": "bot_message", "text": "from a bot", "bot_id": "B99"}]
    signals, _ = SlackConnector.parse(raw, {}, channel="C1")
    assert signals == []


@pytest.mark.parametrize("subtype", [
    "message_changed", "message_deleted", "channel_join", "thread_broadcast"])
def test_parse_skips_subtyped_messages(subtype):
    raw = [_msg("1710000001.000000", subtype=subtype)]
    signals, _ = SlackConnector.parse(raw, {}, channel="C1")
    assert signals == []


def test_parse_skips_thread_replies_keeps_thread_parents():
    raw = [
        # A reply inside a thread: thread_ts differs from ts.
        _msg("1710000002.000000", text="a reply",
             thread_ts="1710000001.000000"),
        # A thread parent: thread_ts equals ts; still a top-level message.
        _msg("1710000001.000000", text="the parent",
             thread_ts="1710000001.000000"),
    ]
    signals, _ = SlackConnector.parse(raw, {}, channel="C1")
    assert [s.payload["message"] for s in signals] == ["the parent"]


def test_parse_skips_configured_bot_user():
    raw = [_msg("1710000001.000000", user="UBOT"),
           _msg("1710000002.000000", user="UHUMAN")]
    signals, _ = SlackConnector.parse(raw, {}, channel="C1", bot_user_id="UBOT")
    assert [s.payload["user"] for s in signals] == ["UHUMAN"]


def test_parse_skips_empty_text_and_missing_ts():
    raw = [_msg("1710000001.000000", text="   "),
           {"type": "message", "text": "no ts", "user": "U1"},
           "not-a-dict"]
    signals, _ = SlackConnector.parse(raw, {}, channel="C1")
    assert signals == []


# --- parse: cursor -----------------------------------------------------------

def test_cursor_advances_and_repoll_of_same_raw_is_noop():
    raw = [_msg("1710000002.000000"), _msg("1710000001.000000")]
    signals, state = SlackConnector.parse(raw, {}, channel="C1")
    assert len(signals) == 2
    assert state["last_ts"] == "1710000002.000000"
    signals2, state2 = SlackConnector.parse(raw, state, channel="C1")
    assert signals2 == []
    assert state2["last_ts"] == "1710000002.000000"


def test_cursor_emits_only_newer_messages():
    _, state = SlackConnector.parse([_msg("1710000001.000000")], {}, channel="C1")
    raw = [_msg("1710000002.000000", text="new"),
           _msg("1710000001.000000", text="old")]
    signals, state2 = SlackConnector.parse(raw, state, channel="C1")
    assert [s.payload["message"] for s in signals] == ["new"]
    assert state2["last_ts"] == "1710000002.000000"


def test_cursor_advances_past_filtered_messages():
    # Bot chatter newer than every user message must still move the watermark,
    # so the next poll's oldest= excludes it server-side.
    raw = [_msg("1710000002.000000", bot_id="B9"),
           _msg("1710000001.000000", text="real")]
    signals, state = SlackConnector.parse(raw, {}, channel="C1")
    assert len(signals) == 1
    assert state["last_ts"] == "1710000002.000000"


def test_cursor_ts_comparison_is_numeric_not_lexical():
    _, state = SlackConnector.parse([_msg("1710000000.000009")], {}, channel="C1")
    raw = [_msg("1710000000.000010", text="newer")]
    signals, state2 = SlackConnector.parse(raw, state, channel="C1")
    assert [s.payload["message"] for s in signals] == ["newer"]
    assert state2["last_ts"] == "1710000000.000010"


# --- parse: partial fetch (429 / page cap) ------------------------------------

def test_partial_fetch_emits_but_does_not_advance_watermark():
    # history is newest-first: an incomplete fetch may have a gap between the
    # cursor and the oldest fetched page, so the watermark must hold still.
    raw = {"messages": [_msg("1710000005.000000", text="newest")],
           "complete": False}
    signals, state = SlackConnector.parse(raw, {"last_ts": "1710000001.000000"},
                                          channel="C1")
    assert [s.payload["message"] for s in signals] == ["newest"]
    assert state["last_ts"] == "1710000001.000000"  # unchanged
    assert "slack-C1-1710000005.000000" in state["seen"]


def test_partial_then_complete_poll_dedups_and_advances():
    partial = {"messages": [_msg("1710000005.000000", text="newest")],
               "complete": False}
    _, state = SlackConnector.parse(partial, {}, channel="C1")
    # Next poll re-covers the full range including the gap message.
    complete = {"messages": [_msg("1710000005.000000", text="newest"),
                             _msg("1710000003.000000", text="gap")],
                "complete": True}
    signals, state2 = SlackConnector.parse(complete, state, channel="C1")
    assert [s.payload["message"] for s in signals] == ["gap"]
    assert state2["last_ts"] == "1710000005.000000"


def test_empty_complete_fetch_preserves_state():
    state = {"last_ts": "1710000001.000000", "seen": ["slack-C1-1710000001.000000"]}
    signals, state2 = SlackConnector.parse({"messages": [], "complete": True},
                                           state, channel="C1")
    assert signals == []
    assert state2["last_ts"] == "1710000001.000000"


# --- fetch: pagination + rate limit -------------------------------------------

def _src(**cfg):
    return SourceRef(name="feedback", type="slack", team="dev",
                     config={"channel": "C1", **cfg}, secret="xoxb-token")


def _response(status, payload=None, headers=None):
    request = httpx.Request("GET", "https://slack.com/api/conversations.history")
    return httpx.Response(status, json=payload or {}, headers=headers or {},
                          request=request)


def test_fetch_paginates_with_next_cursor(monkeypatch):
    pages = [
        {"ok": True, "messages": [_msg("1710000002.000000")],
         "response_metadata": {"next_cursor": "cur2"}},
        {"ok": True, "messages": [_msg("1710000001.000000")],
         "response_metadata": {"next_cursor": ""}},
    ]
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append({"url": url, "headers": headers, "params": dict(params)})
        return _response(200, pages[len(calls) - 1])

    monkeypatch.setattr(httpx, "get", fake_get)
    raw = SlackConnector().fetch(_src(), {"last_ts": "1710000000.000000"})
    assert raw["complete"] is True
    assert [m["ts"] for m in raw["messages"]] == [
        "1710000002.000000", "1710000001.000000"]
    assert len(calls) == 2
    assert calls[0]["url"] == "https://slack.com/api/conversations.history"
    assert calls[0]["headers"]["Authorization"] == "Bearer xoxb-token"
    assert calls[0]["params"]["channel"] == "C1"
    assert calls[0]["params"]["oldest"] == "1710000000.000000"
    assert "cursor" not in calls[0]["params"]
    assert calls[1]["params"]["cursor"] == "cur2"


def test_fetch_omits_oldest_on_first_poll(monkeypatch):
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(dict(params))
        return _response(200, {"ok": True, "messages": []})

    monkeypatch.setattr(httpx, "get", fake_get)
    SlackConnector().fetch(_src(), {})
    assert "oldest" not in calls[0]


def test_fetch_429_returns_partial_without_retrying(monkeypatch):
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(dict(params))
        if len(calls) == 1:
            return _response(200, {
                "ok": True, "messages": [_msg("1710000002.000000")],
                "response_metadata": {"next_cursor": "cur2"}})
        return _response(429, {}, headers={"Retry-After": "30"})

    monkeypatch.setattr(httpx, "get", fake_get)
    raw = SlackConnector().fetch(_src(), {})
    assert raw["complete"] is False
    assert [m["ts"] for m in raw["messages"]] == ["1710000002.000000"]
    assert len(calls) == 2  # no busy-loop, no in-call retry


def test_fetch_429_on_first_page_returns_empty_incomplete(monkeypatch):
    monkeypatch.setattr(httpx, "get",
                        lambda *a, **k: _response(429, {}, {"Retry-After": "5"}))
    raw = SlackConnector().fetch(_src(), {})
    assert raw == {"messages": [], "complete": False}


def test_fetch_stops_at_page_cap_and_marks_incomplete(monkeypatch):
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(1)
        return _response(200, {
            "ok": True, "messages": [_msg(f"171000000{len(calls)}.000000")],
            "response_metadata": {"next_cursor": f"cur{len(calls)}"}})

    monkeypatch.setattr(httpx, "get", fake_get)
    raw = SlackConnector().fetch(_src(max_pages=3), {})
    assert len(calls) == 3
    assert raw["complete"] is False
    assert len(raw["messages"]) == 3


def test_fetch_raises_on_api_error_envelope(monkeypatch):
    monkeypatch.setattr(httpx, "get",
                        lambda *a, **k: _response(200, {"ok": False,
                                                        "error": "invalid_auth"}))
    with pytest.raises(RuntimeError, match="invalid_auth"):
        SlackConnector().fetch(_src(), {})


def test_fetch_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _response(500))
    with pytest.raises(httpx.HTTPStatusError):
        SlackConnector().fetch(_src(), {})


def test_fetch_noops_without_channel_or_token(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no HTTP call expected")

    monkeypatch.setattr(httpx, "get", boom)
    no_channel = SourceRef(name="f", type="slack", team="dev", config={},
                           secret="xoxb")
    assert SlackConnector().fetch(no_channel, {}) == {"messages": [],
                                                      "complete": True}
    no_token = SourceRef(name="f", type="slack", team="dev",
                         config={"channel": "C1"})
    assert SlackConnector().fetch(no_token, {}) == {"messages": [],
                                                    "complete": True}


# --- poll ---------------------------------------------------------------------

def test_poll_wires_channel_and_options_from_config():
    class FakeSlack(SlackConnector):
        def fetch(self, source, state):
            return {"messages": [_msg("1710000001.000000", text="hi", user="U7"),
                                 _msg("1710000002.000000", user="UBOT")],
                    "complete": True}

    src = _src(bot_user_id="UBOT", workspace="acme")
    signals, state = FakeSlack().poll(src, {})
    assert len(signals) == 1
    assert signals[0].fingerprint == "slack-C1-1710000001.000000"
    assert signals[0].reply_to == {"kind": "slack_thread", "channel": "C1",
                                   "ts": "1710000001.000000"}
    assert signals[0].payload["permalink"].startswith("https://acme.slack.com/")
    assert state["last_ts"] == "1710000002.000000"


# --- driver integration --------------------------------------------------------

def test_driver_ingests_slack_signals_with_reply_to(conn, tmp_path, monkeypatch):
    class FakeSlack(SlackConnector):
        def fetch(self, source, state):
            return {"messages": [_msg("1710000002.000000", text="B"),
                                 _msg("1710000001.000000", text="A")],
                    "complete": True}

    from argus.v2.connectors import base

    monkeypatch.setitem(base.REGISTRY, "slack", FakeSlack)
    y = tmp_path / "a.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: dev\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "    sources:\n"
        "      - name: feedback\n        type: slack\n        scope: team\n"
        "        config: { channel: C1, respond: true }\n",
        encoding="utf-8",
    )
    cfg = loader.load(y)
    assert driver.poll_once(conn, cfg) == 2
    assert driver.poll_once(conn, cfg) == 0  # cursor persisted; re-poll is a no-op
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload->>'fingerprint', payload->'reply_to' "
            "FROM events ORDER BY received_at")
        rows = cur.fetchall()
    assert [r[0] for r in rows] == ["slack-C1-1710000001.000000",
                                    "slack-C1-1710000002.000000"]
    assert rows[0][1] == {"kind": "slack_thread", "channel": "C1",
                          "ts": "1710000001.000000"}
    with conn.cursor() as cur:
        cur.execute("SELECT cursor FROM connector_state WHERE source_name='feedback'")
        cursor = cur.fetchone()[0]
    assert cursor["last_ts"] == "1710000002.000000"


def test_driver_rate_limited_poll_resumes_next_time(conn, tmp_path, monkeypatch):
    polls = {"n": 0}

    class FlakySlack(SlackConnector):
        def fetch(self, source, state):
            polls["n"] += 1
            if polls["n"] == 1:
                # 429 mid-pagination: newest page only, incomplete.
                return {"messages": [_msg("1710000002.000000", text="new")],
                        "complete": False}
            return {"messages": [_msg("1710000002.000000", text="new"),
                                 _msg("1710000001.000000", text="older")],
                    "complete": True}

    from argus.v2.connectors import base

    monkeypatch.setitem(base.REGISTRY, "slack", FlakySlack)
    y = tmp_path / "a.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: dev\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "    sources:\n"
        "      - name: feedback\n        type: slack\n        scope: team\n"
        "        config: { channel: C1 }\n",
        encoding="utf-8",
    )
    cfg = loader.load(y)
    assert driver.poll_once(conn, cfg) == 1   # partial: newest message only
    assert driver.poll_once(conn, cfg) == 1   # resume: the gap message lands
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM events")
        assert cur.fetchone()[0] == 2
