import json
from datetime import datetime, timezone

from argus.engine import EngineOutageError
from argus.v2.context import remind, state


def _env(monkeypatch, tmp_path, rows=None):
    monkeypatch.setenv("ARGUS_CONTEXT_DIR", str(tmp_path / "context"))
    monkeypatch.setenv("ARGUS_CONTEXT_SOURCE", "echo")
    monkeypatch.setenv("ARGUS_CONTEXT_ECHO_ROWS", "\n".join(json.dumps(row) for row in (rows or [])))
    monkeypatch.setenv("ARGUS_CONTEXT_WA_TO", "owner@s.whatsapp.net")


def test_remind_extracts_and_delivers_due_commitment(tmp_path, monkeypatch, conn):
    _env(monkeypatch, tmp_path, [
        {"id": 1, "who": "A", "body": "I will send the report", "ts": "2026-06-17T10:00:00Z"},
    ])
    sends = []

    def engine(_prompt: str) -> str:
        return json.dumps({"who": "A", "what": "send the report", "due_at": "2026-06-17"})

    result = remind.run(
        now_iso="2026-06-17T12:00:00Z",
        engine_runner=engine,
        sender=lambda to, text: sends.append((to, text)) or True,
    )

    assert result.delivered == 1
    assert state.get_watermark("remind") == 1
    assert sends == [(
        "owner@s.whatsapp.net",
        "Reminder - you committed to:\n- send the report (by 2026-06-17)",
    )]
    assert state.commit_list("open") == []
    surfaced = state.commit_list("surfaced")
    assert surfaced[0]["what"] == "send the report"


def test_remind_non_commitment_advances_without_engine(tmp_path, monkeypatch, conn):
    _env(monkeypatch, tmp_path, [
        {"id": 5, "who": "A", "body": "just chatting", "ts": "2026-06-17"},
    ])

    result = remind.run(
        now_iso="2026-06-17T12:00:00Z",
        engine_runner=lambda _prompt: (_ for _ in ()).throw(AssertionError()),
        sender=lambda _to, _text: True,
    )

    assert result.no_due is True
    assert state.get_watermark("remind") == 5


def test_remind_extraction_outage_still_surfaces_stored_due_item(tmp_path, monkeypatch, conn):
    _env(monkeypatch, tmp_path, [
        {"id": 2, "who": "A", "body": "I will call you", "ts": "2026-06-17"},
    ])
    state.commit_upsert("A", "stored task", "2026-06-17", "manual:1")
    sends = []

    result = remind.run(
        now_iso="2026-06-17T12:00:00Z",
        engine_runner=lambda _prompt: (_ for _ in ()).throw(EngineOutageError("down")),
        sender=lambda to, text: sends.append((to, text)) or True,
    )

    assert result.extraction_outage is True
    assert result.delivered == 1
    assert state.get_watermark("remind") == 0
    assert "stored task" in sends[0][1]
    with conn.cursor() as cur:
        cur.execute("SELECT fingerprint, channel FROM alerts")
        assert cur.fetchone() == ("context-remind-outage", "whatsapp")


def test_remind_missing_destination_records_error(tmp_path, monkeypatch, conn):
    _env(monkeypatch, tmp_path)
    monkeypatch.delenv("ARGUS_CONTEXT_WA_TO", raising=False)
    state.commit_upsert("A", "stored task", "2026-06-17", "manual:1")

    result = remind.run(
        now_iso="2026-06-17T12:00:00Z",
        sender=lambda _to, _text: (_ for _ in ()).throw(AssertionError()),
    )

    assert result.failed is True
    with conn.cursor() as cur:
        cur.execute("SELECT fingerprint, channel FROM alerts")
        assert cur.fetchone() == ("context-remind-noto", "whatsapp")


def test_should_surface_respects_snooze_and_missing_due():
    now = datetime(2026, 6, 17, 12, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    assert remind.should_surface("2026-06-17", "", now, 48) is True
    assert remind.should_surface("", "", now, 48) is False
    assert remind.should_surface("2026-06-17", "2026-06-18T00:00:00Z", now, 48) is False
