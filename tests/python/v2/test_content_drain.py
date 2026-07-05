import json

from argus.v2.content import drain, state


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_CONTENT_DIR", str(tmp_path / "content"))
    monkeypatch.setenv("ARGUS_WA_TO", "owner@s.whatsapp.net")
    monkeypatch.setenv("ARGUS_CONTENT_IMAGE_DRIVER", "echo")


def _engine(prompt: str) -> str:
    if "Produce the brief" in prompt:
        return json.dumps({"angle": "launch"})
    if "Write the post" in prompt:
        return json.dumps({"body": "hello from content"})
    if "Write the image prompt" in prompt:
        return "clean image prompt"
    if "Return the verdict" in prompt:
        return json.dumps({"verdict": "pass"})
    raise AssertionError(prompt)


def test_content_drain_noops_without_queue(tmp_path, monkeypatch, conn):
    _env(monkeypatch, tmp_path)

    result = drain.run(engine_runner=lambda _prompt: (_ for _ in ()).throw(AssertionError()))

    assert result.no_queue is True


def test_content_drain_creates_draft_and_marks_queue(tmp_path, monkeypatch, conn):
    _env(monkeypatch, tmp_path)
    queue_id = state.queue_add("luma", "linkedin", "announce launch")
    notifications = []

    result = drain.run(
        engine_runner=_engine,
        notifier=lambda draft_id, body, to, image: notifications.append((draft_id, body, to, image)) or True,
    )

    assert result.failed is False
    assert result.draft_id
    assert state.queue_latest(queue_id)["status"] == "drafted"
    draft = state.latest_draft(result.draft_id)
    assert draft["status"] == "ready"
    draft_dir = tmp_path / "content" / result.draft_id
    assert (draft_dir / "brief.json").exists()
    assert (draft_dir / "copy.txt").read_text(encoding="utf-8") == "hello from content"
    assert notifications[0][1] == "hello from content"
    assert notifications[0][2] == "owner@s.whatsapp.net"
    assert notifications[0][3].name == "image.png"


def test_content_ready_notification_requires_readiness_proof(monkeypatch):
    sent = []
    monkeypatch.setattr(drain, "_send_text", lambda to, text: sent.append((to, text)) or True)
    monkeypatch.setattr(drain, "_send_media", lambda *args, **kwargs: True)

    assert drain._default_notifier("draft-1", "body", "owner@s.whatsapp.net", None) is True

    assert "Publish only after approval proof" in sent[0][1]
    assert "Metricool target" in sent[0][1]
    assert '"publish draft-1"' not in sent[0][1]


def test_content_drain_dead_letters_after_third_failure(tmp_path, monkeypatch, conn):
    _env(monkeypatch, tmp_path)
    queue_id = state.queue_add("luma", "linkedin", "announce launch")
    state.queue_set_status(queue_id, "queued", 2)

    result = drain.run(engine_runner=lambda _prompt: (_ for _ in ()).throw(RuntimeError("down")))

    assert result.failed is True
    latest = state.queue_latest(queue_id)
    assert latest["status"] == "dead"
    assert latest["attempts"] == 3
    with conn.cursor() as cur:
        cur.execute("SELECT fingerprint, channel FROM alerts")
        assert cur.fetchone() == (f"content-queue-{queue_id}", "log")
