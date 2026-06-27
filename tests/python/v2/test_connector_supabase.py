import pytest

from argus.v2.connectors import driver
from argus.v2.connectors.supabase import SupabaseConnector, _name
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
