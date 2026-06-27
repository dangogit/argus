import json

from argus.v2.channels import receiver, fake
from argus.v2.config import loader
from argus.v2.orchestrator import reconcile
from argus.v2.actions import executor
from argus.v2.worker import worker


def _cfg(tmp_path):
    y = tmp_path / "a.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo }, webhook_secret: s3cret }\n"
        "teams:\n  - name: dev\n    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "    channels: [ { type: fake, role: control, channel_id: 'chatA' } ]\n")
    return loader.load(y)


def test_inbound_chat_chitchat_replies_back_out(conn, tmp_path):
    fake.SENT.clear()
    cfg = _cfg(tmp_path)
    body = json.dumps({"chat_id": "chatA", "id": "m1", "text": "thanks team"}).encode()
    receiver.handle_webhook(conn, cfg, "fake", body, {"x-argus-secret": "s3cret"}); conn.commit()
    for _ in range(4):
        reconcile.sweep_once(conn, cfg); conn.commit()
        while worker.run_once(cfg, "w1"):
            pass
    # "thanks" is chitchat -> the front emits a reply -> delivered to the chat.
    assert any(chat == "chatA" for chat, _ in fake.SENT)
