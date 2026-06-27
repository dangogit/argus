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


def test_registry_has_fake():
    assert "fake" in base.REGISTRY


def test_routing_matches_binding(tmp_path):
    cfg = _cfg(tmp_path)
    assert router.team_for(cfg, "fake", "chatA")[0] == "dev"
    assert router.team_for(cfg, "fake", "chatB")[0] == "mkt"
    assert router.team_for(cfg, "fake", "nope") is None


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
