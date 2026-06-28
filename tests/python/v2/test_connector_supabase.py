import pytest

from argus.v2.connectors import driver
from argus.v2.connectors.supabase import SupabaseConnector, _build_url, _name
from argus.v2.config import loader
from argus.v2.config.schema import SourceRef


def test_parse_maps_rows_to_legacy_finding_shape():
    rows = [
        {
            "id": 101,
            "title": "Avatar upload fails",
            "severity": "high",
            "url": "https://example.test/bugs/101",
            "created_at": "2026-06-17T01:02:03Z",
        }
    ]
    signals, state = SupabaseConnector.parse(
        rows,
        {},
        project="demo",
        table="bug_reports",
        severity_column="severity",
    )
    assert [s.fingerprint for s in signals] == ["supabase-demo-bug_reports-101"]
    payload = signals[0].payload
    assert payload["source"] == "supabase"
    assert payload["severity"] == "error"
    assert payload["message"] == "Avatar upload fails"
    assert payload["kind"] == "bug"
    assert payload["last_seen"] == "2026-06-17T01:02:03Z"
    assert state["seen"] == ["supabase-demo-bug_reports-101"]


def test_parse_uses_description_fallback_and_dedups_seen_rows():
    rows = [{"id": "abc", "description": "Broken checkout"}]
    signals, state = SupabaseConnector.parse(rows, {}, project="demo")
    assert signals[0].payload["message"] == "Broken checkout"
    signals2, state2 = SupabaseConnector.parse(rows, state, project="demo")
    assert signals2 == []
    assert state2 == state


def test_cursor_advances_and_dedups_standing_rows():
    # A standing-match table returns the SAME rows every poll. With the cursor
    # persisted, the second poll yields zero new signals.
    rows = [
        {"id": "a", "title": "fail A", "created_at": "2026-06-17T01:00:00Z"},
        {"id": "b", "title": "fail B", "created_at": "2026-06-17T02:00:00Z"},
    ]
    signals, state = SupabaseConnector.parse(rows, {}, project="demo", table="t")
    assert [s.fingerprint for s in signals] == ["supabase-demo-t-a", "supabase-demo-t-b"]
    assert state["watermark"] == "2026-06-17T02:00:00Z"  # max created_at

    signals2, state2 = SupabaseConnector.parse(rows, state, project="demo", table="t")
    assert signals2 == []  # nothing new on a re-poll of the same standing rows
    assert state2["watermark"] == "2026-06-17T02:00:00Z"


def test_cursor_survives_seen_reset():
    # State loss that wipes `seen` but keeps the watermark must NOT re-flood
    # already-old rows: the cursor excludes everything strictly below it. (The
    # exact-watermark boundary is excluded server-side by gt.<watermark>; see
    # test_build_url_adds_cursor_filter_and_order.)
    rows = [
        {"id": "a", "title": "fail A", "created_at": "2026-06-17T01:00:00Z"},
        {"id": "b", "title": "fail B", "created_at": "2026-06-17T02:00:00Z"},
    ]
    state = {"seen": [], "watermark": "2026-06-17T09:00:00Z"}  # later than all rows
    signals, _ = SupabaseConnector.parse(rows, state, project="demo", table="t")
    assert signals == []  # cursor excludes rows below the watermark even with seen lost


def test_cursor_emits_only_newer_rows():
    _, state = SupabaseConnector.parse(
        [{"id": "a", "created_at": "2026-06-17T01:00:00Z"}], {}, project="demo", table="t")
    newer = [
        {"id": "a", "created_at": "2026-06-17T01:00:00Z"},   # old, already seen
        {"id": "c", "created_at": "2026-06-17T03:00:00Z"},   # new
    ]
    signals, state2 = SupabaseConnector.parse(newer, state, project="demo", table="t")
    assert [s.fingerprint for s in signals] == ["supabase-demo-t-c"]
    assert state2["watermark"] == "2026-06-17T03:00:00Z"


def test_parse_without_cursor_column_falls_back_to_seen():
    rows = [{"id": "a", "title": "no timestamp here"}]
    signals, state = SupabaseConnector.parse(rows, {}, project="demo", table="t",
                                             cursor_column=None)
    assert [s.fingerprint for s in signals] == ["supabase-demo-t-a"]
    assert "watermark" not in state  # no cursor tracked
    assert state["seen"] == ["supabase-demo-t-a"]


def test_build_url_adds_cursor_filter_and_order():
    url = _build_url("https://x.supabase.co", "transactions", "status=eq.failed", 25,
                     cursor_column="created_at", watermark="2026-06-17T02:00:00+00:00")
    assert "status=eq.failed" in url
    assert "created_at=gt.2026-06-17T02%3A00%3A00%2B00%3A00" in url  # url-encoded
    assert "order=created_at.asc" in url
    assert url.endswith("limit=25")


def test_build_url_no_cursor_when_disabled():
    url = _build_url("https://x.supabase.co", "t", "status=eq.open", 10,
                     cursor_column=None, watermark="2026-06-17T02:00:00Z")
    assert "gt." not in url and "order=" not in url


def test_name_rejects_unsafe_table():
    with pytest.raises(ValueError):
        _name("bug_reports;drop")


def test_poll_noops_without_url_or_secret():
    src = SourceRef(name="bugs", type="supabase", team="dev", config={"project": "demo"})
    signals, state = SupabaseConnector().poll(src, {})
    assert signals == []
    assert state == {"seen": []}


def test_driver_ingests_supabase_signals(conn, tmp_path, monkeypatch):
    class FakeSupabase(SupabaseConnector):
        def fetch(self, source, state):
            return [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}]

    from argus.v2.connectors import base

    monkeypatch.setitem(base.REGISTRY, "supabase", FakeSupabase)
    y = tmp_path / "a.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "  sources:\n"
        "    - name: sb\n      type: supabase\n      scope: company\n      team: dev\n"
        "      config: { project: demo, url: 'https://example.supabase.co' }\n"
        "teams:\n"
        "  - name: dev\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n",
        encoding="utf-8",
    )
    cfg = loader.load(y)
    assert driver.poll_once(conn, cfg) == 2
    assert driver.poll_once(conn, cfg) == 0
    with conn.cursor() as cur:
        cur.execute("SELECT payload->>'fingerprint' FROM events ORDER BY received_at")
        assert [r[0] for r in cur.fetchall()] == [
            "supabase-demo-bug_reports-1",
            "supabase-demo-bug_reports-2",
        ]
