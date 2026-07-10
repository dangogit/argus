"""Respond-back to signal origins (orchestrator/respond.py): when a signal's
work reaches a terminal state (request done/failed, or triaged as ignore /
no_fix), Argus proposes a short reply to the signal's 'reply_to' origin
through the actions outbox. Opt-in per source via config `respond: true`
(supabase's `writeback: true` keeps implying it). New origin kinds register
in respond.RESPONDERS.
"""
import json
from types import SimpleNamespace

from psycopg.types.json import Json

from argus.v2.channels import send
from argus.v2.config import loader
from argus.v2.connectors import driver
from argus.v2.connectors import base as connectors_base
from argus.v2.connectors.supabase import SupabaseConnector
from argus.v2.ingress import events
from argus.v2.orchestrator import pipeline, reconcile, respond
from argus.v2.queue import jobs


def _cfg(tmp_path, *, respond_on=True, source_type="webhook"):
    src_cfg = {"respond": True} if respond_on else {}
    y = tmp_path / "c.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "  sources:\n"
        f"    - {{ name: hook, type: {source_type}, scope: company, team: dev, "
        f"config: {json.dumps(src_cfg)} }}\n"
        "teams:\n  - name: dev\n"
        "    project: { repo: /tmp/x, base_branch: main, test_cmd: 'true' }\n"
        "    roles: [ { name: developer, kind: builder, prompt: p },\n"
        "             { name: qa, kind: judge, prompt: p },\n"
        "             { name: senior, kind: judge, prompt: p } ]\n"
        "    pipeline: { stages: [developer, qa, senior], max_iters: 2 }\n"
    )
    return loader.load(y)


_SLACK_ORIGIN = {"kind": "slack_thread", "channel": "C123", "ts": "111.222"}


def _terminal_signal_request(conn, cfg, *, status, dedup, reply_to=_SLACK_ORIGIN,
                             with_pr=False):
    payload = {"message": "x"}
    if reply_to:
        payload["reply_to"] = reply_to
    eid = events.ingest_signal(conn, cfg, team="dev", source="hook",
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
                (rid, f"open_pr:{rid}", "https://github.com/o/r/pull/7"))
    conn.commit()
    return rid


# --- slack_thread responder via terminal requests ---

def test_slack_reply_proposed_for_done_request_with_pr(conn, tmp_path):
    cfg = _cfg(tmp_path)
    rid = _terminal_signal_request(conn, cfg, status="done", dedup="s1", with_pr=True)
    n = respond.sweep(conn, cfg); conn.commit()
    assert n == 1
    with conn.cursor() as cur:
        cur.execute("SELECT risk, destination_ref, idempotency_key, payload "
                    "FROM actions WHERE type='reply'")
        risk, dest, idem, payload = cur.fetchone()
    assert risk == "reversible_internal"
    assert dest == "slack:C123"
    assert idem == f"respond:{rid}"
    assert payload["thread_ts"] == "111.222"
    assert "pull/7" in payload["text"]


def test_failed_request_gets_needs_human_note(conn, tmp_path):
    cfg = _cfg(tmp_path)
    _terminal_signal_request(conn, cfg, status="failed", dedup="s2")
    respond.sweep(conn, cfg); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT payload->>'text' FROM actions WHERE type='reply'")
        note = cur.fetchone()[0].lower()
    assert "did not pass" in note or "needs a human" in note


def test_respond_default_off(conn, tmp_path):
    cfg = _cfg(tmp_path, respond_on=False)
    _terminal_signal_request(conn, cfg, status="done", dedup="s3")
    assert respond.sweep(conn, cfg) == 0


def test_respond_idempotent(conn, tmp_path):
    cfg = _cfg(tmp_path)
    _terminal_signal_request(conn, cfg, status="done", dedup="s4")
    n1 = respond.sweep(conn, cfg); conn.commit()
    n2 = respond.sweep(conn, cfg); conn.commit()
    assert (n1, n2) == (1, 0)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM actions WHERE type='reply'")
        assert cur.fetchone()[0] == 1


def test_signal_without_reply_to_is_skipped(conn, tmp_path):
    cfg = _cfg(tmp_path)
    _terminal_signal_request(conn, cfg, status="done", dedup="s5", reply_to=None)
    assert respond.sweep(conn, cfg) == 0


def test_unknown_origin_kind_is_skipped(conn, tmp_path):
    cfg = _cfg(tmp_path)
    _terminal_signal_request(conn, cfg, status="done", dedup="s6",
                             reply_to={"kind": "carrier_pigeon", "coop": "north"})
    assert respond.sweep(conn, cfg) == 0


# --- ignored signals (triage decided not to act) ---

def _ignored_signal(conn, cfg, *, dedup, decision="ignore", text="", kind="triage",
                    job_status="done"):
    eid = events.ingest_signal(conn, cfg, team="dev", source="hook",
                               fingerprint=dedup,
                               payload={"message": "x", "reply_to": _SLACK_ORIGIN})
    jid = jobs.enqueue(conn, team_id="dev", kind=kind, role="manager", stage=0,
                       idempotency_key=f"{kind}:dev:{dedup}",
                       exec_snapshot={"engine": "echo"},
                       payload={"text": "t", "fingerprint": dedup},
                       request_id=None, event_id=eid, conversation_id=None)
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET status=%s WHERE id=%s", (job_status, jid))
        # The marker _triage_marker writes for a handled triage/research job.
        payload = {"triage": decision}
        if text:
            payload["text"] = text
        cur.execute(
            "INSERT INTO actions (job_id, team_id, type, risk, idempotency_key, "
            " status, payload) "
            "VALUES (%s,'dev','notify','reversible_internal',%s,'done',%s)",
            (jid, f"{kind}:{jid}", Json(payload)))
    conn.commit()
    return eid


def test_ignored_signal_gets_reply_with_reason(conn, tmp_path):
    cfg = _cfg(tmp_path)
    eid = _ignored_signal(conn, cfg, dedup="i1", decision="ignore",
                          text="expected during the deploy window")
    n = respond.sweep(conn, cfg); conn.commit()
    assert n == 1
    with conn.cursor() as cur:
        cur.execute("SELECT idempotency_key, payload FROM actions WHERE type='reply'")
        idem, payload = cur.fetchone()
    assert idem == f"respond:{eid}"
    assert "decided not to act" in payload["text"]
    assert "deploy window" in payload["text"]


def test_research_no_fix_gets_reply(conn, tmp_path):
    cfg = _cfg(tmp_path)
    _ignored_signal(conn, cfg, dedup="i2", decision="no_fix", kind="research")
    n = respond.sweep(conn, cfg); conn.commit()
    assert n == 1
    with conn.cursor() as cur:
        cur.execute("SELECT payload->>'text' FROM actions WHERE type='reply'")
        assert "no automated code fix" in cur.fetchone()[0]


def test_dispatched_triage_does_not_reply_early(conn, tmp_path):
    """A triage that dispatched (marker note 'dispatch') must not trigger the
    ignored-signal reply; the terminal-request path answers later."""
    cfg = _cfg(tmp_path)
    _ignored_signal(conn, cfg, dedup="i3", decision="dispatch")
    assert respond.sweep(conn, cfg) == 0


def test_engine_failed_triage_does_not_reply(conn, tmp_path):
    """pipeline._handle_triage writes an ignore marker even when the triage job
    itself failed (engine error fallback). That marker is not a real decision:
    no reply may be sent for it."""
    cfg = _cfg(tmp_path)
    _ignored_signal(conn, cfg, dedup="i5", decision="ignore", job_status="failed")
    assert respond.sweep(conn, cfg) == 0


def test_ignored_reply_idempotent(conn, tmp_path):
    cfg = _cfg(tmp_path)
    _ignored_signal(conn, cfg, dedup="i4")
    n1 = respond.sweep(conn, cfg); conn.commit()
    n2 = respond.sweep(conn, cfg); conn.commit()
    assert (n1, n2) == (1, 0)


# --- supabase responder rides the same mechanism ---

def _sb_cfg(tmp_path, *, writeback=True, respond_on=False):
    import os
    os.environ["SB_KEY"] = "test-key"
    cfgd = {"url": "https://x.supabase.co", "table": "bug_reports",
            "id_column": "id", "notes_column": "admin_notes"}
    if writeback:
        cfgd["writeback"] = True
    if respond_on:
        cfgd["respond"] = True
    y = tmp_path / "sb.yaml"
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


def _terminal_sb_request(conn, cfg, *, dedup, payload):
    eid = events.ingest_signal(conn, cfg, team="dev", source="sb-bugs",
                               fingerprint=dedup, payload=payload)
    rid = pipeline.open_request(conn, cfg, event_id=eid, team_id="dev",
                                conversation_id=None, fingerprint=dedup)
    with conn.cursor() as cur:
        cur.execute("UPDATE requests SET status='done' WHERE id=%s", (rid,))
    conn.commit()
    return rid


def test_supabase_reply_to_routes_bug_writeback(conn, tmp_path):
    """A new-style supabase signal (reply_to with row_id, no legacy 'row' key)
    still produces the classic bug_writeback action with the classic key."""
    cfg = _sb_cfg(tmp_path)
    rid = _terminal_sb_request(conn, cfg, dedup="n1", payload={
        "message": "x",
        "reply_to": {"kind": "supabase_bug_reports", "row_id": "row-42"}})
    n = respond.sweep(conn, cfg); conn.commit()
    assert n == 1
    with conn.cursor() as cur:
        cur.execute("SELECT idempotency_key, payload FROM actions "
                    "WHERE type='bug_writeback'")
        idem, payload = cur.fetchone()
    assert idem == f"bug_writeback:{rid}"
    assert payload["source_name"] == "sb-bugs"
    assert payload["row_id"] == "row-42"


def test_supabase_respond_optin_works_without_writeback_flag(conn, tmp_path):
    cfg = _sb_cfg(tmp_path, writeback=False, respond_on=True)
    _terminal_sb_request(conn, cfg, dedup="n2", payload={
        "message": "x",
        "reply_to": {"kind": "supabase_bug_reports", "row_id": "row-1"}})
    assert respond.sweep(conn, cfg) == 1


def test_reconcile_alias_still_answers_legacy_writeback(conn, tmp_path):
    """Legacy signals (payload 'row', no reply_to) written before this feature
    still get their write-back through the alias reconcile keeps exposing."""
    cfg = _sb_cfg(tmp_path)
    _terminal_sb_request(conn, cfg, dedup="n3",
                         payload={"message": "x", "row": {"id": "row-9"}})
    n = reconcile.writeback_terminal_bugs(conn, cfg); conn.commit()
    assert n == 1
    with conn.cursor() as cur:
        cur.execute("SELECT payload->>'row_id' FROM actions WHERE type='bug_writeback'")
        assert cur.fetchone()[0] == "row-9"


def test_supabase_respond_e2e_patches_row(conn, tmp_path, monkeypatch):
    """End to end for a TEAM-scoped source with only respond: true (no
    writeback flag): sweep proposes the bug_writeback action AND executing it
    through handlers.run really PATCHes the row (the proposal must not be
    skipped at delivery)."""
    import os
    from argus.v2.actions import handlers
    os.environ["SB_KEY"] = "test-key"
    cfgd = {"url": "https://x.supabase.co", "table": "bug_reports",
            "id_column": "id", "notes_column": "admin_notes", "respond": True}
    y = tmp_path / "sbteam.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n  - name: dev\n"
        "    project: { repo: /tmp/x, base_branch: main, test_cmd: 'true' }\n"
        "    sources:\n"
        f"      - {{ name: sb-bugs, type: supabase, scope: team, "
        f"secret_ref: '${{env:SB_KEY}}', config: {json.dumps(cfgd)} }}\n"
        "    roles: [ { name: developer, kind: builder, prompt: p },\n"
        "             { name: qa, kind: judge, prompt: p },\n"
        "             { name: senior, kind: judge, prompt: p } ]\n"
        "    pipeline: { stages: [developer, qa, senior], max_iters: 2 }\n"
    )
    cfg = loader.load(y)
    _terminal_sb_request(conn, cfg, dedup="n4", payload={
        "message": "x",
        "reply_to": {"kind": "supabase_bug_reports", "row_id": "row-77"}})
    assert respond.sweep(conn, cfg) == 1; conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM actions WHERE type='bug_writeback'")
        payload = cur.fetchone()[0]
    captured = {}
    monkeypatch.setattr(handlers, "_sb_get",
                        lambda url, headers: [{"admin_notes": ""}])
    def fake_patch(url, headers, body):
        captured["url"] = url; captured["body"] = body; return [{"id": "row-77"}]
    monkeypatch.setattr(handlers, "_sb_patch", fake_patch)
    ref = handlers.run("bug_writeback", payload, cfg=cfg, team_id="dev")
    assert "skip" not in ref.lower()
    assert "row-77" in captured["url"]
    assert captured["body"]["admin_notes"]


# --- Signal.reply_to persistence ---

def test_supabase_connector_populates_reply_to():
    raw = [{"id": "b7", "title": "boom", "created_at": "2026-01-01"}]
    signals, _state = SupabaseConnector.parse(raw, {}, project="p")
    assert signals[0].reply_to == {"kind": "supabase_bug_reports", "row_id": "b7"}


def test_driver_persists_reply_to_into_event_payload(conn, tmp_path):
    class _StubConnector:
        type = "stubrespond"

        def poll(self, source, state):
            sig = connectors_base.Signal(
                fingerprint="stub-1", payload={"message": "x"},
                reply_to={"kind": "slack_thread", "channel": "C9", "ts": "1.2"})
            return [sig], {}

    connectors_base.REGISTRY["stubrespond"] = _StubConnector
    try:
        cfg = _cfg(tmp_path, source_type="stubrespond")
        driver.poll_once(conn, cfg, source_names={"hook"})
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT payload FROM events WHERE dedup_key='stub-1'")
            payload = cur.fetchone()[0]
        assert payload["reply_to"] == {"kind": "slack_thread", "channel": "C9",
                                       "ts": "1.2"}
    finally:
        connectors_base.REGISTRY.pop("stubrespond", None)


# --- threaded delivery seam ---

def test_deliver_passes_thread_ts_to_thread_capable_channel(monkeypatch):
    captured = {}

    class _ThreadChannel:
        type = "threadcap"

        def send(self, binding, text, thread_ts=None):
            captured.update(binding=binding, text=text, thread_ts=thread_ts)
            return "ts-1"

    from argus.v2.channels import base as chbase, router
    chbase.REGISTRY["threadcap"] = _ThreadChannel
    binding = SimpleNamespace(channel_id="C1", secret="s")
    monkeypatch.setattr(router, "team_for", lambda cfg, t, c, text=None: ("dev", binding))
    try:
        ref = send.deliver(None, "threadcap:C1", "hello", thread_ts="9.9")
    finally:
        chbase.REGISTRY.pop("threadcap", None)
    assert ref == "ts-1"
    assert captured["thread_ts"] == "9.9"


def test_deliver_falls_back_for_channels_without_thread_support(monkeypatch):
    from argus.v2.channels import fake as fake_channel, router
    fake_channel.SENT.clear()
    binding = SimpleNamespace(channel_id="C1", secret=None)
    monkeypatch.setattr(router, "team_for", lambda cfg, t, c, text=None: ("dev", binding))
    ref = send.deliver(None, "fake:C1", "hello", thread_ts="9.9")
    assert ref == "fake-1"
    assert fake_channel.SENT == [("C1", "hello")]


# --- wiring ---

def test_slack_thread_and_supabase_responders_registered():
    assert "slack_thread" in respond.RESPONDERS
    assert respond.SUPABASE_KIND in respond.RESPONDERS
