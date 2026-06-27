import sys
import time

from argus.v2.advisor import state, tick


GROUP = "120363000000000001@g.us"
BOT = "555666777888"


def seed(jid: str, message_id: str, participant: str, body: str,
         *, now: int, age: int = 300, quoted: str = "",
         participant_jid: str | None = None) -> None:
    row = {
        "ts": now - age,
        "id": message_id,
        "participant": participant,
        "participant_jid": participant_jid or f"{participant}@s.whatsapp.net",
        "push_name": "Alice",
        "body": body,
        "mentioned": [],
        "quoted_participant": quoted,
    }
    state.record_message(jid, row)


def env(monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_ADVISOR_DIR", str(tmp_path / "advisor"))
    monkeypatch.setenv("ARGUS_ADVISOR_GROUPS", GROUP)
    monkeypatch.setenv("ARGUS_ADVISOR_BOT_IDS", BOT)
    monkeypatch.setenv("ARGUS_ADVISOR_COALESCE_SEC", "0")


def safe_engine(prompt: str) -> str:
    if "binary safety classifier" in prompt:
        return "SAFE"
    return "canned advisor answer"


def test_mentioned_message_replies_and_advances_cursor(tmp_path, monkeypatch, conn):
    env(monkeypatch, tmp_path)
    now = int(time.time())
    seed(GROUP, "M1", "111", f"@{BOT} what is argus?", now=now)
    sends = []

    processed = tick.run(now=now, engine_runner=safe_engine,
                         sender=lambda *args: sends.append(args) or True)

    assert processed == 1
    assert state.cursor(GROUP) == 1
    assert state.replies(GROUP)[0]["reply_to_id"] == "M1"
    assert sends[0][1] == "canned advisor answer"
    assert sends[0][2] == "M1"


def test_non_mention_is_consumed_silently(tmp_path, monkeypatch, conn):
    env(monkeypatch, tmp_path)
    now = int(time.time())
    seed(GROUP, "M1", "111", "just chatting", now=now)

    processed = tick.run(now=now, engine_runner=safe_engine,
                         sender=lambda *args: (_ for _ in ()).throw(AssertionError()))

    assert processed == 1
    assert state.cursor(GROUP) == 1
    assert state.replies(GROUP) == []


def test_young_message_waits_for_coalesce(tmp_path, monkeypatch, conn):
    env(monkeypatch, tmp_path)
    monkeypatch.setenv("ARGUS_ADVISOR_COALESCE_SEC", "600")
    now = int(time.time())
    seed(GROUP, "M1", "111", f"@{BOT} early", now=now, age=10)

    processed = tick.run(now=now, engine_runner=safe_engine, sender=lambda *args: True)

    assert processed == 0
    assert state.cursor(GROUP) == 0


def test_burst_from_same_participant_merges_one_reply(tmp_path, monkeypatch, conn):
    env(monkeypatch, tmp_path)
    now = int(time.time())
    seen = {}
    seed(GROUP, "M1", "111", f"@{BOT} part one", now=now)
    seed(GROUP, "M2", "111", "part two", now=now)

    def engine(prompt: str) -> str:
        if "binary safety classifier" in prompt:
            seen["abuse"] = prompt
            return "SAFE"
        seen["reply"] = prompt
        return "combined"

    tick.run(now=now, engine_runner=engine, sender=lambda *args: True)

    assert "part one\npart two" in seen["reply"]
    assert state.cursor(GROUP) == 2
    assert len([r for r in state.replies(GROUP) if not r.get("skipped")]) == 1


def test_user_hourly_cap_records_skip(tmp_path, monkeypatch, conn):
    env(monkeypatch, tmp_path)
    monkeypatch.setenv("ARGUS_ADVISOR_USER_HOURLY", "1")
    now = int(time.time())
    state.record_reply(GROUP, {"ts": now - 60, "participant": "111", "reply_to_id": "M0", "parts": 1})
    seed(GROUP, "M1", "111", f"@{BOT} again", now=now)

    tick.run(now=now, engine_runner=safe_engine, sender=lambda *args: True)

    skips = [r for r in state.replies(GROUP) if r.get("skipped")]
    assert skips[-1]["reason"] == "rate_user"


def test_abuse_verdict_skips_and_records_warn(tmp_path, monkeypatch, conn):
    env(monkeypatch, tmp_path)
    now = int(time.time())
    seed(GROUP, "M1", "111", f"@{BOT} bad", now=now)

    tick.run(now=now, engine_runner=lambda _prompt: "UNSAFE", sender=lambda *args: True)

    assert state.replies(GROUP)[0]["reason"] == "abuse"
    with conn.cursor() as cur:
        cur.execute("SELECT severity, channel FROM alerts")
        assert cur.fetchone() == ("warn", "log")


def test_send_failure_retries_then_dead_letters(tmp_path, monkeypatch, conn):
    env(monkeypatch, tmp_path)
    monkeypatch.setenv("ARGUS_ADVISOR_MAX_ATTEMPTS", "2")
    now = int(time.time())
    seed(GROUP, "M1", "111", f"@{BOT} q", now=now)

    tick.run(now=now, engine_runner=safe_engine, sender=lambda *args: False)
    assert state.cursor(GROUP) == 0
    tick.run(now=now + 10, engine_runner=safe_engine, sender=lambda *args: False)

    assert state.cursor(GROUP) == 1
    assert state.replies(GROUP)[0]["reason"] == "delivery"


def test_long_reply_splits_into_capped_bubbles(tmp_path, monkeypatch, conn):
    env(monkeypatch, tmp_path)
    monkeypatch.setenv("ARGUS_ADVISOR_MAX_REPLY_CHARS", "20")
    monkeypatch.setenv("ARGUS_ADVISOR_MAX_REPLY_PARTS", "2")
    now = int(time.time())
    seed(GROUP, "M1", "111", f"@{BOT} long", now=now)
    sends = []

    def engine(prompt: str) -> str:
        if "binary safety classifier" in prompt:
            return "SAFE"
        return "first part of the answer\nsecond part of the answer"

    tick.run(now=now, engine_runner=engine, sender=lambda *args: sends.append(args) or True)

    assert len(sends) == 2
    assert sends[0][2] == "M1"
    assert sends[1][2] is None
    assert state.replies(GROUP)[0]["parts"] == 2


def test_send_reads_apikey_file(tmp_path, monkeypatch):
    key_file = tmp_path / "apikey"
    key_file.write_text("secret\n", encoding="utf-8")
    seen = {}

    class Response:
        def raise_for_status(self):
            return None

    def fake_post(url, headers, json, timeout):
        seen["url"] = url
        seen["headers"] = headers
        seen["json"] = json
        return Response()

    monkeypatch.setenv("ARGUS_WA_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("ARGUS_WA_INSTANCE", "argus-inbound")
    monkeypatch.setenv("ARGUS_WA_APIKEY_FILE", str(key_file))
    monkeypatch.delenv("ARGUS_WA_APIKEY", raising=False)
    monkeypatch.setitem(sys.modules, "httpx", type("Httpx", (), {"post": fake_post}))

    assert tick._send(GROUP, "hello") is True
    assert seen["headers"]["apikey"] == "secret"
