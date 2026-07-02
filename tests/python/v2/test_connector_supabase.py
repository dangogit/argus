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


def test_fetch_uses_shared_client_and_still_parses(monkeypatch):
    """Smoke test: fetch() now goes through connectors.client.fetch_json instead
    of a hand-rolled httpx.get call, but the request shape and the parsed result
    must be unchanged."""
    import httpx

    rows = [{"id": 1, "title": "A", "created_at": "2026-06-17T01:00:00Z"}]
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append({"url": url, "headers": headers, "timeout": timeout})
        request = httpx.Request("GET", url)
        return httpx.Response(200, json=rows, request=request)

    monkeypatch.setattr(httpx, "get", fake_get)
    src = SourceRef(name="sb", type="supabase", team="dev",
                    config={"project": "demo", "url": "https://example.supabase.co"},
                    secret="key")

    data = SupabaseConnector().fetch(src, {})

    assert data == rows
    assert len(calls) == 1
    assert calls[0]["url"].startswith("https://example.supabase.co/rest/v1/bug_reports?")
    assert calls[0]["headers"] == {"apikey": "key", "Authorization": "Bearer key"}
    assert calls[0]["timeout"] == 15.0

    signals, _ = SupabaseConnector.parse(data, {}, project="demo")
    assert [s.fingerprint for s in signals] == ["supabase-demo-bug_reports-1"]


# --- batch mode ---

def test_batch_off_by_default_still_emits_one_signal_per_row():
    rows = [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}]
    signals, _ = SupabaseConnector.parse(rows, {}, project="demo", table="bug_reports")
    assert [s.fingerprint for s in signals] == [
        "supabase-demo-bug_reports-1",
        "supabase-demo-bug_reports-2",
    ]


def test_batch_true_collapses_n_rows_into_one_signal():
    rows = [
        {"id": 1, "title": "Avatar upload fails", "severity": "high"},
        {"id": 2, "title": "Checkout crashes", "severity": "critical"},
        {"id": 3, "title": "Login loop", "severity": "low"},
    ]
    signals, state = SupabaseConnector.parse(
        rows, {}, project="demo", table="bug_reports", severity_column="severity",
        batch=True,
    )
    assert len(signals) == 1
    sig = signals[0]
    payload = sig.payload
    assert payload["kind"] == "bug_batch"
    assert payload["bug_count"] == 3
    assert len(payload["rows"]) == 3
    assert [f["row"]["id"] for f in payload["rows"]] == [1, 2, 3]
    # worst severity across the batch surfaces at the top level
    assert payload["severity"] == "critical"
    # seen/watermark state still advances so re-polls dedup per-row as usual
    assert len(state["seen"]) == 3


def test_batch_dedup_key_is_stable_and_order_independent():
    rows_a = [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}]
    rows_b = [{"id": 2, "title": "B"}, {"id": 1, "title": "A"}]
    sig_a, _ = SupabaseConnector.parse(rows_a, {}, project="demo", table="t", batch=True)
    sig_b, _ = SupabaseConnector.parse(rows_b, {}, project="demo", table="t", batch=True)
    assert sig_a[0].fingerprint == sig_b[0].fingerprint


def test_batch_repoll_of_same_open_rows_does_not_redispatch():
    """Re-polling the SAME set of open bug rows must produce the identical batch
    fingerprint, so ingest_signal's (source, dedup_key) unique constraint
    collapses it to a no-op re-dispatch (see test_driver_batch_mode_ingests_a_
    single_event below for the end-to-end version through the events table)."""
    rows = [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}]
    sig1, state1 = SupabaseConnector.parse(rows, {}, project="demo", table="t", batch=True)
    # A fresh poll cycle (e.g. after a restart) re-fetches the same still-open
    # rows with fresh state; the fingerprint must still match.
    sig2, _ = SupabaseConnector.parse(rows, {}, project="demo", table="t", batch=True)
    assert sig1[0].fingerprint == sig2[0].fingerprint


def test_batch_different_bug_set_gets_a_new_fingerprint():
    rows_before = [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}]
    rows_after = [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}, {"id": 3, "title": "C"}]
    sig_before, _ = SupabaseConnector.parse(rows_before, {}, project="demo", table="t", batch=True)
    sig_after, _ = SupabaseConnector.parse(rows_after, {}, project="demo", table="t", batch=True)
    assert sig_before[0].fingerprint != sig_after[0].fingerprint


def test_batch_no_signal_when_no_open_rows():
    signals, state = SupabaseConnector.parse([], {}, project="demo", table="t", batch=True)
    assert signals == []


def test_batch_only_includes_new_rows_not_already_seen():
    rows = [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}]
    _, state = SupabaseConnector.parse(rows, {}, project="demo", table="t", batch=True)
    # Row 1 stays open (still returned), row 3 is newly opened.
    rows2 = [{"id": 1, "title": "A"}, {"id": 3, "title": "C"}]
    signals2, _ = SupabaseConnector.parse(rows2, state, project="demo", table="t", batch=True)
    assert len(signals2) == 1
    assert [f["row"]["id"] for f in signals2[0].payload["rows"]] == [3]


def test_poll_reads_batch_from_source_config():
    class FakeSupabase(SupabaseConnector):
        def fetch(self, source, state):
            return [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}]

    src = SourceRef(name="sb", type="supabase", team="dev",
                    config={"project": "demo", "url": "https://x.supabase.co", "batch": True})
    signals, _ = FakeSupabase().poll(src, {})
    assert len(signals) == 1
    assert signals[0].payload["kind"] == "bug_batch"


def test_driver_batch_mode_ingests_a_single_event(conn, tmp_path, monkeypatch):
    class FakeSupabase(SupabaseConnector):
        def fetch(self, source, state):
            return [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}, {"id": 3, "title": "C"}]

    from argus.v2.connectors import base

    monkeypatch.setitem(base.REGISTRY, "supabase", FakeSupabase)
    y = tmp_path / "a.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "  sources:\n"
        "    - name: sb\n      type: supabase\n      scope: company\n      team: dev\n"
        "      config: { project: demo, url: 'https://example.supabase.co', batch: true }\n"
        "teams:\n"
        "  - name: dev\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n",
        encoding="utf-8",
    )
    cfg = loader.load(y)
    assert driver.poll_once(conn, cfg) == 1  # one ingested signal for 3 bugs
    with conn.cursor() as cur:
        cur.execute("SELECT payload->>'kind', payload->'bug_count' FROM events")
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "bug_batch"
    assert rows[0][1] == 3
    # A re-poll of the SAME open rows does not create a second event.
    assert driver.poll_once(conn, cfg) == 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM events")
        assert cur.fetchone()[0] == 1


def test_fetch_falls_back_on_missing_cursor_column(monkeypatch):
    """The 42703 (undefined column) fallback path still works with fetch_json:
    the first call errors, the retry without cursor_column succeeds."""
    import httpx

    rows = [{"id": 5, "title": "B"}]
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(url)
        request = httpx.Request("GET", url)
        if "created_at" in url:
            return httpx.Response(
                400, json={"message": "column bug_reports.created_at does not exist",
                          "code": "42703"},
                request=request)
        return httpx.Response(200, json=rows, request=request)

    monkeypatch.setattr(httpx, "get", fake_get)
    src = SourceRef(name="sb", type="supabase", team="dev",
                    config={"project": "demo", "url": "https://example.supabase.co"},
                    secret="key")

    data = SupabaseConnector().fetch(src, {})

    assert data == rows
    assert len(calls) == 2
    assert "created_at" in calls[0]
    assert "created_at" not in calls[1]
