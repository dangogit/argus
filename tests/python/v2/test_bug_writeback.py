"""Supabase bug-report write-back (2026-06-19): when a request that originated
from a supabase bug source reaches a terminal state, Argus writes its verdict
back to the bug row's notes column so the loop closes (the row stops looking
ignored). Opt-in per source via config `writeback: true`. The write is an
outbox action (reversible_internal -> auto-runs), proposed by reconcile.
"""
import json

import pytest

from argus.v2.actions import executor, handlers
from argus.v2.config import loader
from argus.v2.ingress import events
from argus.v2.orchestrator import pipeline, reconcile


def _cfg(tmp_path, *, writeback=True):
    import os
    os.environ["SB_KEY"] = "test-key"
    cfgd = {"url": "https://x.supabase.co", "table": "bug_reports",
            "id_column": "id", "notes_column": "admin_notes", "writeback": writeback}
    y = tmp_path / "c.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "  sources:\n"
        f"    - {{ name: sb-bugs, type: supabase, scope: company, team: dev, "
        f"secret_ref: '${{env:SB_KEY}}', config: {json.dumps(cfgd)} }}\n"
        "teams:\n  - name: dev\n"
        "    project: { repo: /tmp/x, base_branch: main, test_cmd: 'true' }\n"
        "    roles: [ { name: developer, kind: builder, prompt: p },\n"
        "             { name: qa, kind: judge, prompt: p },\n"
        "             { name: senior, kind: judge, prompt: p } ]\n"
        "    pipeline: { stages: [developer, qa, senior], max_iters: 2 }\n"
    )
    return loader.load(y)


# --- pure note composition ---

def test_compose_appends_to_existing():
    assert handlers.compose_writeback_notes("old", "new") == "old\n\nnew"


@pytest.mark.parametrize("cur", ["", "   ", None])
def test_compose_empty_current(cur):
    assert handlers.compose_writeback_notes(cur, "new") == "new"


# --- reconcile proposer ---

def _terminal_supabase_request(conn, cfg, *, status, dedup, row_id="bug-1", with_pr=False):
    eid = events.ingest_signal(conn, cfg, team="dev", source="sb-bugs",
                               fingerprint=dedup,
                               payload={"message": "x", "row": {"id": row_id}})
    rid = pipeline.open_request(conn, cfg, event_id=eid, team_id="dev",
                                conversation_id=None, fingerprint=dedup)
    with conn.cursor() as cur:
        cur.execute("UPDATE requests SET status=%s WHERE id=%s", (status, rid))
        if with_pr:
            cur.execute(
                "INSERT INTO actions (request_id, team_id, type, risk, "
                " idempotency_key, provider_ref, status) "
                "VALUES (%s,'dev','open_pr','reversible_internal',%s,%s,'done')",
                (rid, f"open_pr:{rid}", "https://github.com/o/r/pull/9"))
    conn.commit()
    return rid


def test_writeback_proposed_for_done_with_pr(conn, tmp_path):
    cfg = _cfg(tmp_path)
    _terminal_supabase_request(conn, cfg, status="done", dedup="b1",
                               row_id="row-9", with_pr=True)
    n = reconcile.writeback_terminal_bugs(conn, cfg); conn.commit()
    assert n == 1
    with conn.cursor() as cur:
        cur.execute("SELECT risk, payload FROM actions WHERE type='bug_writeback'")
        risk, payload = cur.fetchone()
    assert risk == "reversible_internal"
    assert payload["source_name"] == "sb-bugs"
    assert payload["row_id"] == "row-9"
    assert "pull/9" in payload["note"]


def test_writeback_failed_note(conn, tmp_path):
    cfg = _cfg(tmp_path)
    _terminal_supabase_request(conn, cfg, status="failed", dedup="b2")
    reconcile.writeback_terminal_bugs(conn, cfg); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT payload->>'note' FROM actions WHERE type='bug_writeback'")
        note = cur.fetchone()[0].lower()
    assert "did not pass" in note or "could not" in note


def test_writeback_done_nofix_note(conn, tmp_path):
    cfg = _cfg(tmp_path)
    _terminal_supabase_request(conn, cfg, status="done", dedup="b2b", with_pr=False)
    reconcile.writeback_terminal_bugs(conn, cfg); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT payload->>'note' FROM actions WHERE type='bug_writeback'")
        note = cur.fetchone()[0].lower()
    assert "no automated code fix" in note or "no code fix" in note


def test_writeback_skipped_when_disabled(conn, tmp_path):
    cfg = _cfg(tmp_path, writeback=False)
    _terminal_supabase_request(conn, cfg, status="done", dedup="b3")
    n = reconcile.writeback_terminal_bugs(conn, cfg); conn.commit()
    assert n == 0


def test_writeback_idempotent(conn, tmp_path):
    cfg = _cfg(tmp_path)
    _terminal_supabase_request(conn, cfg, status="done", dedup="b4")
    reconcile.writeback_terminal_bugs(conn, cfg); conn.commit()
    n2 = reconcile.writeback_terminal_bugs(conn, cfg); conn.commit()
    assert n2 == 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM actions WHERE type='bug_writeback'")
        assert cur.fetchone()[0] == 1


# --- handler (PATCH via injected http) ---

def test_handler_patches_appended_notes(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    captured = {}
    monkeypatch.setattr(handlers, "_sb_get", lambda url, headers: [{"admin_notes": "prior"}])
    def fake_patch(url, headers, body):
        captured["url"] = url; captured["body"] = body; return [{"id": "row-9"}]
    monkeypatch.setattr(handlers, "_sb_patch", fake_patch)
    ref = handlers.run("bug_writeback",
                       {"source_name": "sb-bugs", "row_id": "row-9", "note": "Argus did X"},
                       cfg=cfg, team_id="dev")
    assert captured["body"]["admin_notes"] == "prior\n\nArgus did X"
    assert "row-9" in captured["url"]
    assert "writeback" in ref


def test_handler_skips_when_writeback_disabled(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, writeback=False)
    monkeypatch.setattr(handlers, "_sb_patch",
                        lambda *a, **k: pytest.fail("must not PATCH when disabled"))
    ref = handlers.run("bug_writeback",
                       {"source_name": "sb-bugs", "row_id": "r", "note": "x"},
                       cfg=cfg, team_id="dev")
    assert "skip" in ref.lower()


# --- wiring ---

def test_bug_writeback_is_registered_reversible():
    assert "bug_writeback" in executor._REAL
    assert executor.risk_for("bug_writeback") == "reversible_internal"
