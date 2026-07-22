"""Typed human interactions: ask / confirm / suggest over conversation contexts."""
from argus.v2.actions import executor
from argus.v2.config import loader
from argus.v2.ingress import events
from argus.v2.orchestrator import interactions, pipeline, reconcile


def _cfg(tmp_path, project: bool = False):
    proj = ("    project: { repo: /tmp/x, base_branch: main, test_cmd: 'true' }\n"
            if project else "")
    y = tmp_path / "i.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults:\n"
        "    engine: { engine: echo }\n"
        "    autonomy: { reversible_internal: auto, irreversible_outward: approval }\n"
        "teams:\n  - name: dev\n" + proj +
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "    channels: [ { type: whatsapp, role: control, channel_id: 'grp1' } ]\n")
    return loader.load(y)


def _reply(conn, cfg, text, key="m1"):
    events.ingest_message(conn, cfg, team="dev", source="whatsapp:grp1",
                          dedup_key=key, text=text,
                          conversation_key="whatsapp:grp1")
    conn.commit()
    reconcile.route_events(conn, cfg)
    conn.commit()


def _one(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _pending_approval(conn, cfg):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO actions (team_id, type, risk, idempotency_key, payload) "
            "VALUES ('dev','merge','irreversible_outward','a0','{\"text\":\"merge PR 7\"}') "
            "RETURNING id")
        aid = str(cur.fetchone()[0])
    executor.process_proposed(conn, cfg)
    conn.commit()
    nonce = _one(conn, "SELECT nonce FROM approvals WHERE action_id=%s", (aid,))[0]
    return aid, nonce


# --- confirm -----------------------------------------------------------------

def test_approval_posts_confirm_prompt_to_control_channel(conn, tmp_path):
    cfg = _cfg(tmp_path)
    aid, nonce = _pending_approval(conn, cfg)
    dest, text = _one(conn, "SELECT destination_ref, payload->>'text' FROM actions "
                            "WHERE type='notify'")
    assert dest == "whatsapp:grp1"
    assert "Approval needed" in text and "merge PR 7" in text and nonce in text
    ctype, ref, payload = _one(conn, "SELECT context_type, context_ref, payload "
                                     "FROM conversation_contexts")
    assert ctype == "interaction" and ref == f"approval:{nonce}"
    assert payload["kind"] == "confirm" and payload["nonce"] == nonce


def test_confirm_approve_via_chat_executes_action(conn, tmp_path):
    cfg = _cfg(tmp_path)
    aid, nonce = _pending_approval(conn, cfg)
    _reply(conn, cfg, "approve")
    assert _one(conn, "SELECT status FROM approvals WHERE nonce=%s", (nonce,))[0] == "approved"
    executor.process_proposed(conn, cfg)
    conn.commit()
    assert _one(conn, "SELECT status FROM actions WHERE id=%s", (aid,))[0] == "done"
    reply = _one(conn, "SELECT payload->>'text' FROM actions WHERE type='reply'")[0]
    assert "Approved" in reply
    assert _one(conn, "SELECT status FROM conversation_contexts")[0] == "resolved"


def test_confirm_reject_via_chat_rejects_action(conn, tmp_path):
    cfg = _cfg(tmp_path)
    aid, nonce = _pending_approval(conn, cfg)
    _reply(conn, cfg, "reject")
    assert _one(conn, "SELECT status FROM actions WHERE id=%s", (aid,))[0] == "rejected"
    reply = _one(conn, "SELECT payload->>'text' FROM actions WHERE type='reply'")[0]
    assert "Rejected" in reply


def test_confirm_unrelated_text_falls_through_keeps_pending(conn, tmp_path):
    cfg = _cfg(tmp_path)
    aid, nonce = _pending_approval(conn, cfg)
    _reply(conn, cfg, "what is the weather")
    assert _one(conn, "SELECT status FROM approvals WHERE nonce=%s", (nonce,))[0] == "pending"
    assert _one(conn, "SELECT status FROM actions WHERE id=%s", (aid,))[0] == "awaiting_approval"
    assert _one(conn, "SELECT status FROM conversation_contexts")[0] == "active"


def test_confirm_on_consumed_nonce_reports_no_longer_pending(conn, tmp_path):
    cfg = _cfg(tmp_path)
    aid, nonce = _pending_approval(conn, cfg)
    from argus.v2.actions import approvals
    approvals.consume(conn, nonce, decision="rejected", approver_ref="cli")
    conn.commit()
    _reply(conn, cfg, "approve")
    reply = _one(conn, "SELECT payload->>'text' FROM actions WHERE type='reply'")[0]
    assert "no longer pending" in reply
    assert _one(conn, "SELECT status FROM actions WHERE id=%s", (aid,))[0] == "rejected"


# --- suggest -----------------------------------------------------------------

def _suggest(conn, cfg):
    interactions.open_interaction(
        conn, team_id="dev", channel_ref="whatsapp:grp1", kind="suggest",
        key="sg-1", prompt="Want me to clean up the flaky test?",
        payload={"task": "Clean up the flaky test in tests/x.py",
                 "fingerprint": "SG-1", "yes_reply": "On it, cleaning up."})
    conn.commit()


def test_suggest_yes_opens_prepared_request(conn, tmp_path):
    cfg = _cfg(tmp_path, project=True)
    _suggest(conn, cfg)
    _reply(conn, cfg, "do it")
    count, fingerprint = _one(conn, "SELECT count(*), max(fingerprint) FROM requests")
    assert count == 1 and fingerprint == "SG-1"
    task = _one(conn, "SELECT payload->>'text' FROM events WHERE dedup_key='m1'")[0]
    assert task == "Clean up the flaky test in tests/x.py"
    reply = _one(conn, "SELECT payload->>'text' FROM actions WHERE type='reply'")[0]
    assert "On it, cleaning up." == reply
    assert _one(conn, "SELECT status FROM conversation_contexts")[0] == "resolved"


def test_suggest_no_dismisses_without_request(conn, tmp_path):
    cfg = _cfg(tmp_path, project=True)
    _suggest(conn, cfg)
    _reply(conn, cfg, "not now")
    assert _one(conn, "SELECT count(*) FROM requests")[0] == 0
    assert _one(conn, "SELECT status FROM conversation_contexts")[0] == "expired"


def test_suggest_unrelated_text_falls_through(conn, tmp_path):
    cfg = _cfg(tmp_path, project=True)
    _suggest(conn, cfg)
    _reply(conn, cfg, "how are the metrics today")
    assert _one(conn, "SELECT count(*) FROM requests")[0] == 0
    assert _one(conn, "SELECT status FROM conversation_contexts")[0] == "active"


def test_open_interaction_idempotent_per_key(conn, tmp_path):
    cfg = _cfg(tmp_path)
    _suggest(conn, cfg)
    _suggest(conn, cfg)
    assert _one(conn, "SELECT count(*) FROM actions WHERE type='notify'")[0] == 1
    assert _one(conn, "SELECT count(*) FROM conversation_contexts")[0] == 1


# --- ask ---------------------------------------------------------------------

def test_blocked_close_registers_ask_with_task_and_blocker(conn, tmp_path):
    cfg = _cfg(tmp_path, project=True)
    eid = events.ingest_message(conn, cfg, team="dev", source="cli",
                                dedup_key="t1", text="upgrade the billing lib")
    rid = pipeline.open_request(conn, cfg, event_id=eid, team_id="dev",
                                conversation_id=None)
    conn.commit()
    pipeline._no_fix_close(conn, cfg, rid, "cannot reach the API", blocked=True)
    conn.commit()
    note = _one(conn, "SELECT payload->>'text' FROM actions WHERE type='notify'")[0]
    assert "Blocked" in note and "Reply here with guidance" in note
    payload = _one(conn, "SELECT payload FROM conversation_contexts "
                         "WHERE context_type='interaction'")[0]
    assert payload["kind"] == "ask"
    assert payload["task"] == "upgrade the billing lib"
    assert payload["blocker"] == "cannot reach the API"
    assert payload["request_id"] == str(rid)


def test_ask_reply_reopens_request_with_guidance(conn, tmp_path):
    cfg = _cfg(tmp_path, project=True)
    interactions.open_interaction(
        conn, team_id="dev", channel_ref="whatsapp:grp1", kind="ask",
        key="unblock:r1", prompt=None,
        payload={"request_id": "r1", "team_id": "dev",
                 "task": "upgrade the billing lib",
                 "blocker": "cannot reach the API"})
    conn.commit()
    _reply(conn, cfg, "use the staging API key from the vault")
    count, fingerprint = _one(conn, "SELECT count(*), max(fingerprint) FROM requests")
    assert count == 1 and fingerprint == "unblock:r1"
    task = _one(conn, "SELECT payload->>'text' FROM events WHERE dedup_key='m1'")[0]
    assert "use the staging API key from the vault" in task
    assert "upgrade the billing lib" in task
    assert "cannot reach the API" in task
    reply = _one(conn, "SELECT payload->>'text' FROM actions WHERE type='reply'")[0]
    assert "retrying with your guidance" in reply
    assert _one(conn, "SELECT status FROM conversation_contexts")[0] == "resolved"


def test_ask_dismissal_drops_without_request(conn, tmp_path):
    cfg = _cfg(tmp_path, project=True)
    interactions.open_interaction(
        conn, team_id="dev", channel_ref="whatsapp:grp1", kind="ask",
        key="unblock:r1", prompt=None,
        payload={"request_id": "r1", "team_id": "dev", "task": "t", "blocker": "b"})
    conn.commit()
    _reply(conn, cfg, "leave it")
    assert _one(conn, "SELECT count(*) FROM requests")[0] == 0
    assert _one(conn, "SELECT status FROM conversation_contexts")[0] == "expired"
