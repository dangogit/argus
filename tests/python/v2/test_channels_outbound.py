from argus.v2.channels import send, fake
from argus.v2.config import loader


def _cfg(tmp_path):
    y = tmp_path / "a.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n  - name: dev\n    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "    channels: [ { type: fake, role: control, channel_id: 'chatA' } ]\n")
    return loader.load(y)


def test_deliver_sends_via_adapter(tmp_path):
    fake.SENT.clear()
    cfg = _cfg(tmp_path)
    ref = send.deliver(cfg, "fake:chatA", "all done")
    assert ref.startswith("fake-")
    assert fake.SENT == [("chatA", "all done")]


def test_deliver_unknown_destination_returns_none(tmp_path):
    cfg = _cfg(tmp_path)
    assert send.deliver(cfg, "fake:nope", "x") is None
