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


# ---------------------------------------------------------------------------
# Batch mode: one signal for N bugs -> one dispatch/request -> N writebacks.
# ---------------------------------------------------------------------------

def _terminal_batch_request(conn, cfg, *, status, dedup, row_ids, with_pr=False):
    rows = [{"row": {"id": rid}, "message": f"bug {rid}", "severity": "warn"}
            for rid in row_ids]
    payload = {"kind": "bug_batch", "bug_count": len(row_ids), "rows": rows}
    eid = events.ingest_signal(conn, cfg, team="dev", source="sb-bugs",
                               fingerprint=dedup, payload=payload)
    rid = pipeline.open_request(conn, cfg, event_id=eid, team_id="dev",
                                conversation_id=None, fingerprint=dedup)
    with conn.cursor() as cur:
        cur.execute("UPDATE requests SET status=%s WHERE id=%s", (status, rid))
        if with_pr:
            cur.execute(
                "INSERT INTO actions (request_id, team_id, type, risk, "
                " idempotency_key, provider_ref, status) "
                "VALUES (%s,'dev','open_pr','reversible_internal',%s,%s,'done')",
                (rid, f"open_pr:{rid}", "https://github.com/o/r/pull/42"))
    conn.commit()
    return rid


def test_batch_signal_produces_a_single_dispatch_from_n_bugs(conn, tmp_path):
    """A batch payload with 3 bugs goes through open_request exactly once (one
    request/pipeline run), unlike the pre-batch world where 3 separate signals
    would each open their own request."""
    cfg = _cfg(tmp_path)
    rid = _terminal_batch_request(conn, cfg, status="done", dedup="batch-1",
                                  row_ids=["b1", "b2", "b3"], with_pr=True)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM requests")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT id FROM requests")
        assert str(cur.fetchone()[0]) == str(rid)


EVIDENCE_IDS = [
        "supabase-tadam-agents-bug_reports-batch-85c0cf6ee7e7ae71",
        "26dd2ac7f4783dc062ab1453",
        "a64a33be8e81f6c041b3e991",
        "retro-change:0da901e759011fb8053321bf",
        "5a988017a078fef44c36b680",
        "02767ee5e5a09da6a3c70119",
        "retro-change:21c0007a1c65de6259b3492c",
        "21c0007a1c65de6259b3492c",
        "0da901e759011fb8053321bf",
]


def test_batch_prompt_requires_evidence_for_every_supplied_item():
    ids = EVIDENCE_IDS
    payload = {
        "kind": "bug_batch",
        "rows": [{"row": {"id": item}, "message": f"bug {item}"} for item in ids],
    }

    prompt = pipeline._batch_signal_text("sb-bugs", payload)

    assert all(f"id={item} " in prompt for item in ids)
    assert "Do not claim completion until" in prompt
    assert "enumerate every bug by id" in prompt
    assert "disposition and supporting evidence" in prompt
    assert "id=<id> disposition=<disposition> evidence=<evidence>" in prompt


def test_batch_completion_accepts_all_nine_evidence_entries():
    payload = {"kind": "bug_batch", "rows": [
        {"row": {"id": item}} for item in EVIDENCE_IDS
    ]}
    claim = "\n".join(
        f"- id={item} disposition=fixed evidence=test passed" for item in EVIDENCE_IDS
    )

    assert pipeline._incomplete_batch_items(payload, claim) == []


@pytest.mark.parametrize("missing_id", EVIDENCE_IDS)
def test_batch_completion_rejects_each_missing_evidence_entry(missing_id):
    payload = {"kind": "bug_batch", "rows": [
        {"row": {"id": item}} for item in EVIDENCE_IDS
    ]}
    claim = "\n".join(
        f"- id={item} disposition=fixed evidence=test passed"
        for item in EVIDENCE_IDS if item != missing_id
    )

    assert pipeline._incomplete_batch_items(payload, claim) == [missing_id]


@pytest.mark.parametrize("missing_field", ["disposition", "evidence"])
def test_batch_completion_rejects_item_without_required_support(missing_field):
    item = EVIDENCE_IDS[0]
    payload = {"kind": "bug_batch", "rows": [{"row": {"id": item}}]}
    claim = f"- id={item} disposition=fixed evidence=test passed"
    claim = claim.replace(f" {missing_field}=", f" omitted-{missing_field}=")

    assert pipeline._incomplete_batch_items(payload, claim) == [item]


def test_batch_writeback_proposes_one_action_per_bug(conn, tmp_path):
    cfg = _cfg(tmp_path)
    _terminal_batch_request(conn, cfg, status="done", dedup="batch-2",
                            row_ids=["b1", "b2", "b3"], with_pr=True)
    n = reconcile.writeback_terminal_bugs(conn, cfg); conn.commit()
    assert n == 3
    with conn.cursor() as cur:
        cur.execute("SELECT payload->>'row_id', payload->>'note' FROM actions "
                    "WHERE type='bug_writeback' ORDER BY payload->>'row_id'")
        rows = cur.fetchall()
    assert [r[0] for r in rows] == ["b1", "b2", "b3"]
    for _, note in rows:
        assert "pull/42" in note  # same PR verdict note fans out to every bug


def test_batch_writeback_idempotent_across_resweep(conn, tmp_path):
    cfg = _cfg(tmp_path)
    _terminal_batch_request(conn, cfg, status="done", dedup="batch-3",
                            row_ids=["b1", "b2"], with_pr=True)
    n1 = reconcile.writeback_terminal_bugs(conn, cfg); conn.commit()
    n2 = reconcile.writeback_terminal_bugs(conn, cfg); conn.commit()
    assert n1 == 2
    assert n2 == 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM actions WHERE type='bug_writeback'")
        assert cur.fetchone()[0] == 2


def test_batch_writeback_skips_rows_without_id(conn, tmp_path):
    cfg = _cfg(tmp_path)
    payload = {"kind": "bug_batch", "bug_count": 2, "rows": [
        {"row": {"id": "b1"}, "message": "ok"},
        {"row": {}, "message": "missing id"},
    ]}
    eid = events.ingest_signal(conn, cfg, team="dev", source="sb-bugs",
                               fingerprint="batch-4", payload=payload)
    rid = pipeline.open_request(conn, cfg, event_id=eid, team_id="dev",
                                conversation_id=None, fingerprint="batch-4")
    with conn.cursor() as cur:
        cur.execute("UPDATE requests SET status='done' WHERE id=%s", (rid,))
    conn.commit()
    n = reconcile.writeback_terminal_bugs(conn, cfg); conn.commit()
    assert n == 1  # only the row with an id gets a writeback proposed
    with conn.cursor() as cur:
        cur.execute("SELECT payload->>'row_id' FROM actions WHERE type='bug_writeback'")
        assert cur.fetchone()[0] == "b1"


def test_pr_title_for_batch_signal_names_the_bug_count(conn, tmp_path):
    """The PR opened from a batch signal is titled 'Argus: Investigate N open
    bug reports', not a truncated echo of the first bug's text."""
    cfg = _cfg(tmp_path)
    rid = _terminal_batch_request(conn, cfg, status="done", dedup="batch-title",
                                  row_ids=["b1", "b2", "b3", "b4"])
    info = pipeline._pr_info(conn, cfg, rid, cwd=str(tmp_path))
    assert info["title"] == "Argus: Investigate 4 open bug reports"
