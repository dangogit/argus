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


def test_content_queue_read_path_matches_schema(tmp_path, monkeypatch, conn):
    # Guards the failure class in the incident report: a content query selecting a
    # column the migrated schema does not have raises Postgres UndefinedColumn.
    # Every content read must round-trip against the real migrated schema.
    _env(monkeypatch, tmp_path)
    queue_id = state.queue_add("luma", "linkedin", "announce launch")
    assert state.queue_oldest()["id"] == queue_id
    assert state.queue_latest(queue_id)["status"] == "queued"
    assert [row["id"] for row in state.queue_list()] == [queue_id]
    draft_id = state.register("luma", "linkedin")
    assert state.latest_draft(draft_id)["status"] == "ready"
    assert [row["id"] for row in state.draft_list()] == [draft_id]


def test_content_drain_expires_stale_queued_briefs(tmp_path, monkeypatch, conn):
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("ARGUS_CONTENT_QUEUE_MAX_AGE_DAYS", "7")
    stale = state.queue_add("luma", "linkedin", "wedged brief")
    fresh = state.queue_add("luma", "linkedin", "announce launch")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE content_queue SET created_at = now() - interval '8 days' WHERE id=%s",
            (stale,),
        )
    conn.commit()

    result = drain.run(engine_runner=_engine, notifier=lambda *args: True)

    # The wedged head-of-line brief ages out to a terminal status, so the drain
    # is free to process the fresh brief instead of stalling on it forever.
    assert state.queue_latest(stale)["status"] == "dead"
    assert state.queue_latest(fresh)["status"] == "drafted"
    assert result.draft_id
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM alerts WHERE fingerprint=%s",
            (f"content-queue-expired-{stale}",),
        )
        assert cur.fetchone()[0] == 1


def test_content_blocked_notifies_once_per_transition(tmp_path, monkeypatch, conn):
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("ARGUS_CONTENT_DAILY_LIMIT", "0")  # cap 0 => blocked on entry
    state.queue_add("luma", "linkedin", "announce launch")

    first = drain.run(engine_runner=_engine, notifier=lambda *args: True)
    second = drain.run(engine_runner=_engine, notifier=lambda *args: True)

    assert first.blocked is True
    assert first.failed is True
    assert second.blocked is True
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM alerts WHERE fingerprint LIKE 'content-blocked-%%'")
        # one alert across two blocked runs: emitted on transition, not every run
        assert cur.fetchone()[0] == 1


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
