from argus.v2.config import loader
from argus.v2.ingress import events
from argus.v2.orchestrator import context_router, pipeline, reconcile
from argus.v2.support import state
from argus.v2.worker import worker


def _support_cfg(tmp_path):
    cfg_path = tmp_path / "argus.yaml"
    cfg_path.write_text(
        "company:\n"
        "  name: c\n"
        "  defaults: { engine: { engine: echo } }\n"
        "  sources:\n"
        "    - { type: support_apps_script, name: luma-mail, team: luma, secret: k, "
        "config: { url: 'https://support.test', notify_destination: 'cli:local' } }\n"
        "teams:\n"
        "  - name: luma\n"
        "    roles: [ { name: support, kind: worker, prompt: p } ]\n"
        "    pipeline: { stages: [support] }\n"
        "    channels:\n"
        "      - { type: cli, role: control, channel_id: local }\n",
        encoding="utf-8",
    )
    return loader.load(cfg_path)


def _support_manager_cfg(tmp_path):
    cfg_path = tmp_path / "argus-manager.yaml"
    cfg_path.write_text(
        "company:\n"
        "  name: c\n"
        "  defaults: { engine: { engine: echo } }\n"
        "  sources:\n"
        "    - { type: support_apps_script, name: luma-mail, team: luma, secret: k, "
        "config: { url: 'https://support.test', notify_destination: 'cli:local' } }\n"
        "teams:\n"
        "  - name: luma\n"
        "    roles:\n"
        "      - { name: manager, kind: front, prompt: 'You are the manager.', "
        "engine: { engine: scripted } }\n"
        "      - { name: developer, kind: builder, prompt: p }\n"
        "      - { name: qa, kind: judge, prompt: p }\n"
        "      - { name: senior, kind: judge, prompt: p }\n"
        "    pipeline: { stages: [developer, qa, senior] }\n"
        "    channels:\n"
        "      - { type: cli, role: control, channel_id: local }\n",
        encoding="utf-8",
    )
    return loader.load(cfg_path)


def _register_refund_context(conn):
    guidance = state.register_guidance_request(
        "luma",
        "T-refund-manager",
        "customer@example.com",
        "Request for Refund",
        "This looks high-risk. What should we do?",
        "",
        "Customer requested a refund after delayed activation.",
    )
    req = state.guidance_request("luma", guidance.id)
    context_router.register_context(
        conn,
        team_id="luma",
        channel_ref="cli:local",
        context_type="support_case",
        context_ref=guidance.id,
        summary="refund after delayed activation",
        payload=req,
    )
    return guidance


def _drive_converse(conn, cfg):
    for _ in range(4):
        reconcile.route_events(conn, cfg)
        conn.commit()
        while worker.run_once(cfg, "w1"):
            pass
        reconcile.sweep_once(conn, cfg)
        conn.commit()


def test_support_context_followup_routes_before_generic_fallback(
        tmp_path, monkeypatch, conn):
    monkeypatch.setenv("ARGUS_SUPPORT_DIR", str(tmp_path / "support"))
    cfg = _support_cfg(tmp_path)
    guidance = state.register_guidance_request(
        "luma",
        "T-refund",
        "sabina@example.com",
        "Request for Refund",
        "This looks high-risk. What should we do?",
        "",
        "From: sabina@example.com\nPlease refund my unused subscription.",
    )
    req = state.guidance_request("luma", guidance.id)
    context_router.register_context(
        conn,
        team_id="luma",
        channel_ref="cli:local",
        context_type="support_case",
        context_ref=guidance.id,
        summary="refund request",
        payload=req,
    )
    events.ingest_message(
        conn,
        cfg,
        team="luma",
        source="cli",
        dedup_key="ctx1",
        text="what did she request?",
        conversation_key="cli:local",
    )
    conn.commit()

    reconcile.route_events(conn, cfg)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT payload->>'text' FROM actions WHERE type='reply'")
        text = cur.fetchone()[0]
        cur.execute("SELECT status FROM conversation_contexts WHERE context_ref=%s",
                    (guidance.id,))
        status = cur.fetchone()[0]
    assert "Please refund my unused subscription" in text
    assert "No customer reply sent" in text
    assert "Got it" not in text
    assert status == "active"


def test_support_context_operational_message_routes_to_manager(
        tmp_path, monkeypatch, conn):
    monkeypatch.setenv("ARGUS_SUPPORT_DIR", str(tmp_path / "support"))
    cfg = _support_manager_cfg(tmp_path)
    guidance = _register_refund_context(conn)
    monkeypatch.setattr(
        pipeline,
        "_role_snapshot_extra",
        lambda _role: {
            "scripted_output": (
                'ARGUS_RESULT: {"action":"dispatch",'
                '"reply":"I will handle the credit adjustment.",'
                '"task":"Set this customer credits to 0 after verifying the account."}'
            )
        },
    )
    events.ingest_message(
        conn,
        cfg,
        team="luma",
        source="cli",
        dedup_key="ctx-act",
        text="ok i will refund him. but set his credits to 0",
        conversation_key="cli:local",
    )
    conn.commit()

    _drive_converse(conn, cfg)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs WHERE kind='converse'")
        converse_jobs = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM requests WHERE team_id='luma'")
        requests = cur.fetchone()[0]
        cur.execute("SELECT status FROM support_guidance WHERE id=%s", (guidance.id,))
        guidance_status = cur.fetchone()[0]
        cur.execute("SELECT status FROM conversation_contexts WHERE context_ref=%s",
                    (guidance.id,))
        context_status = cur.fetchone()[0]
        cur.execute("SELECT prompt FROM runs WHERE role='manager'")
        prompt = cur.fetchone()[0]
        cur.execute(
            "SELECT payload->'support_context'->>'channel_ref' "
            "FROM jobs WHERE kind='converse'")
        support_channel_ref = cur.fetchone()[0]

    assert converse_jobs == 1
    assert requests == 1
    assert guidance_status == "pending"
    assert context_status == "resolved"
    assert "Request for Refund" in prompt
    assert "Do not choose learn" in prompt
    assert support_channel_ref == "cli:local"


def test_support_context_policy_message_routes_to_manager_learning(
        tmp_path, monkeypatch, conn):
    monkeypatch.setenv("ARGUS_SUPPORT_DIR", str(tmp_path / "support"))
    cfg = _support_manager_cfg(tmp_path)
    guidance = _register_refund_context(conn)
    learned = "For future refunds, verify usage and the 24-hour guarantee first."
    reply = "Understood. I'll use that rule for future refund cases."
    monkeypatch.setattr(
        pipeline,
        "_role_snapshot_extra",
        lambda _role: {
            "scripted_output": (
                'ARGUS_RESULT: {"action":"learn",'
                f'"reply":"{reply}","guidance":"{learned}"}}'
            )
        },
    )
    events.ingest_message(
        conn,
        cfg,
        team="luma",
        source="cli",
        dedup_key="ctx-learn",
        text=learned,
        conversation_key="cli:local",
    )
    conn.commit()

    _drive_converse(conn, cfg)

    with conn.cursor() as cur:
        cur.execute("SELECT status, answer FROM support_guidance WHERE id=%s",
                    (guidance.id,))
        guidance_status, answer = cur.fetchone()
        cur.execute("SELECT status FROM conversation_contexts WHERE context_ref=%s",
                    (guidance.id,))
        context_status = cur.fetchone()[0]
        cur.execute("SELECT content FROM knowledge WHERE source='support-guidance'")
        knowledge = cur.fetchone()[0]
        cur.execute("SELECT payload->>'text' FROM actions "
                    "WHERE type='reply' AND idempotency_key LIKE 'converse:%'")
        manager_reply = cur.fetchone()[0]

    assert guidance_status == "answered"
    assert answer == learned
    assert context_status == "learned"
    assert learned in knowledge
    assert manager_reply == reply
