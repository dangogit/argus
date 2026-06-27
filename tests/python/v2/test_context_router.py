from argus.v2.config import loader
from argus.v2.ingress import events
from argus.v2.orchestrator import context_router, reconcile
from argus.v2.support import state


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
