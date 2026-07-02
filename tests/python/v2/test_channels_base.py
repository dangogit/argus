from argus.v2.channels import base, router
import argus.v2.channels  # registers
from argus.v2.config import loader
from argus.v2.ingress import events


def _cfg(tmp_path):
    y = tmp_path / "a.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: dev\n    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "    channels: [ { type: fake, role: control, channel_id: 'chatA' } ]\n"
        "  - name: mkt\n    roles: [ { name: writer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [writer] }\n"
        "    channels: [ { type: fake, role: control, channel_id: 'chatB' } ]\n")
    return loader.load(y)


def _shared_cfg(tmp_path):
    y = tmp_path / "shared.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: tadam\n    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "    channels: [ { type: fake, role: control, channel_id: 'shared' } ]\n"
        "  - name: tadam-agents\n    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "    channels: [ { type: fake, role: control, channel_id: 'shared' } ]\n")
    return loader.load(y)


def test_registry_has_fake():
    assert "fake" in base.REGISTRY


def test_routing_matches_binding(tmp_path):
    cfg = _cfg(tmp_path)
    assert router.team_for(cfg, "fake", "chatA")[0] == "dev"
    assert router.team_for(cfg, "fake", "chatB")[0] == "mkt"
    assert router.team_for(cfg, "fake", "nope") is None


def test_shared_channel_routes_to_longest_named_team(tmp_path):
    cfg = _shared_cfg(tmp_path)
    route = router.team_for(cfg, "fake", "shared", "status on tadam-agents bugs")
    assert route[0] == "tadam-agents"


def test_shared_channel_routes_to_shorter_team_when_exactly_named(tmp_path):
    cfg = _shared_cfg(tmp_path)
    route = router.team_for(cfg, "fake", "shared", "audit current tadam PRs")
    assert route[0] == "tadam"


def test_shared_channel_ignores_unrouted_text(tmp_path):
    cfg = _shared_cfg(tmp_path)
    assert router.team_for(cfg, "fake", "shared", "audit current PRs") is None


def test_shared_channel_inbound_uses_text_disambiguation(conn, tmp_path):
    cfg = _shared_cfg(tmp_path)
    n = router.inbound(conn, cfg, "fake", {
        "chat_id": "shared",
        "id": "m1",
        "text": "fix tadam agents onboarding",
    })
    conn.commit()

    assert n == 1
    with conn.cursor() as cur:
        cur.execute("SELECT team_id FROM events WHERE source='fake:shared'")
        assert cur.fetchone()[0] == "tadam-agents"


def test_shared_channel_inbound_logs_empty_text_drop(conn, tmp_path, caplog):
    cfg = _shared_cfg(tmp_path)
    with caplog.at_level("WARNING", logger="argus.v2.channels.router"):
        n = router.inbound(conn, cfg, "fake", {
            "chat_id": "shared",
            "id": "m2",
            "text": "",
            "media": [{"kind": "image", "src": "/tmp/a.png"}],
        })
    conn.commit()

    assert n == 0
    assert "ambiguous channel route" in caplog.text
    assert "teams=tadam,tadam-agents" in caplog.text
    assert "text_present=False" in caplog.text
    assert "media_count=1" in caplog.text
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM events WHERE source='fake:shared'")
        assert cur.fetchone()[0] == 0


def test_same_chat_reuses_one_conversation(conn, tmp_path):
    cfg = _cfg(tmp_path)
    e1 = events.ingest_message(conn, cfg, team="dev", source="fake:chatA",
                               dedup_key="m1", text="hi", conversation_key="fake:chatA")
    e2 = events.ingest_message(conn, cfg, team="dev", source="fake:chatA",
                               dedup_key="m2", text="again", conversation_key="fake:chatA")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT conversation_id FROM events WHERE id IN (%s,%s)", (e1, e2))
        convs = {r[0] for r in cur.fetchall()}
    assert len(convs) == 1  # one ongoing thread per chat
