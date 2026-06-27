from pathlib import Path
from types import SimpleNamespace

import httpx

from argus.v2.connectors import driver
from argus.v2.config import loader


def _cfg(tmp_path, signals):
    import json
    y = tmp_path / "a.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "  sources:\n"
        f"    - {{ name: src1, type: fake, scope: company, team: dev, config: {json.dumps({'signals': signals})} }}\n"
        "teams:\n  - name: dev\n    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n")
    return loader.load(y)


def test_poll_ingests_new_signals_as_events(conn, tmp_path):
    cfg = _cfg(tmp_path, [{"fingerprint": "ISSUE-1", "payload": {"e": 1}},
                          {"fingerprint": "ISSUE-2"}])
    n = driver.poll_once(conn, cfg)
    assert n == 2
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM events WHERE kind='signal'")
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT cursor->>'idx' FROM connector_state WHERE source_name='src1'")
        assert cur.fetchone()[0] == "2"


def test_poll_is_idempotent_via_cursor(conn, tmp_path):
    cfg = _cfg(tmp_path, [{"fingerprint": "ISSUE-1"}])
    assert driver.poll_once(conn, cfg) == 1
    assert driver.poll_once(conn, cfg) == 0  # cursor advanced, nothing new
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM events WHERE kind='signal'")
        assert cur.fetchone()[0] == 1


def test_poll_same_fingerprint_dedups_event(conn, tmp_path):
    # Two sources emitting the same fingerprint still collapse on (source,key)
    # only within a source; cross-source same-fingerprint is allowed (distinct
    # source). Here re-emitting within one source after a manual cursor reset
    # must not create a duplicate event.
    cfg = _cfg(tmp_path, [{"fingerprint": "ISSUE-1"}])
    driver.poll_once(conn, cfg)
    with conn.cursor() as cur:
        cur.execute("UPDATE connector_state SET cursor='{}' WHERE source_name='src1'")
    conn.commit()
    driver.poll_once(conn, cfg)  # re-emits ISSUE-1
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM events WHERE source='src1'")
        assert cur.fetchone()[0] == 1  # ingest_signal dedup on (source, dedup_key)


def test_poll_filters_by_source_name(conn):
    cfg = SimpleNamespace(
        company=SimpleNamespace(sources=[
            SimpleNamespace(name="src1", type="fake", team="dev", secret=None,
                            config={"signals": [{"fingerprint": "ISSUE-1"}]}),
            SimpleNamespace(name="src2", type="fake", team="dev", secret=None,
                            config={"signals": [{"fingerprint": "ISSUE-2"}]}),
        ]),
        teams=[],
    )

    assert driver.poll_once(conn, cfg, source_names={"src2"}) == 1
    with conn.cursor() as cur:
        cur.execute("SELECT source FROM events WHERE kind='signal'")
        assert [row[0] for row in cur.fetchall()] == ["src2"]
        cur.execute("SELECT source_name FROM connector_state ORDER BY source_name")
        assert [row[0] for row in cur.fetchall()] == ["src2"]


def test_poll_filters_by_source_type(conn, monkeypatch):
    class OtherFakeConnector:
        def poll(self, source, state):
            return driver.REGISTRY["fake"]().poll(source, state)

    monkeypatch.setitem(driver.REGISTRY, "other_fake", OtherFakeConnector)
    cfg = SimpleNamespace(
        company=SimpleNamespace(sources=[
            SimpleNamespace(name="src1", type="fake", team="dev", secret=None,
                            config={"signals": [{"fingerprint": "ISSUE-1"}]}),
            SimpleNamespace(name="src2", type="other_fake", team="dev", secret=None,
                            config={"signals": [{"fingerprint": "ISSUE-2"}]}),
        ]),
        teams=[],
    )

    assert driver.poll_once(conn, cfg, source_types={"other_fake"}) == 1
    with conn.cursor() as cur:
        cur.execute("SELECT source FROM events WHERE kind='signal'")
        assert [row[0] for row in cur.fetchall()] == ["src2"]
        cur.execute("SELECT source_name FROM connector_state ORDER BY source_name")
        assert [row[0] for row in cur.fetchall()] == ["src2"]


def test_dry_run_reports_without_ingesting_or_saving_cursor(conn, tmp_path):
    cfg = _cfg(tmp_path, [{"fingerprint": "ISSUE-1"}, {"fingerprint": "ISSUE-2"}])
    previews = driver.dry_run(conn, cfg, source_names={"src1"})

    assert len(previews) == 1
    assert previews[0].source == "src1"
    assert previews[0].ok is True
    assert previews[0].count == 2
    assert previews[0].cursor_keys == ("idx",)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM events WHERE kind='signal'")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM connector_state WHERE source_name='src1'")
        assert cur.fetchone()[0] == 0


def test_dry_run_reports_http_status_detail(conn, tmp_path, monkeypatch):
    class BadConnector:
        def poll(self, source, state):
            request = httpx.Request("GET", "https://api.example.test")
            response = httpx.Response(
                404,
                json={"error": {"message": "Project not found"}},
                request=request,
            )
            raise httpx.HTTPStatusError("not found", request=request, response=response)

    cfg = _cfg(tmp_path, [])
    monkeypatch.setitem(driver.REGISTRY, "fake", BadConnector)

    previews = driver.dry_run(conn, cfg, source_names={"src1"})

    assert previews[0].ok is False
    assert previews[0].error_type == "HTTPStatusError HTTP 404 Project not found"
