from argus.v2 import completion_watchdog
from argus.v2.actions import executor
from argus.v2.channels import fake
from argus.v2.config import loader
from argus.v2.ingress import events
from argus.v2.orchestrator import pipeline
from argus.v2.queue import jobs
from argus.v2.queue.models import RunRecord


def _cfg(tmp_path, *, generic=False):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    generic_block = ""
    if generic:
        generic_block = (
            "    completion_watchdog:\n"
            "      enabled: true\n"
            "      threshold_minutes: 45\n"
        )
    path = tmp_path / "argus.yaml"
    path.write_text(
        "company:\n"
        "  name: c\n"
        "  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: content\n"
        f"    project: {{ repo: {repo} }}\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "    channels: [ { type: fake, role: control, channel_id: 'argus-content' } ]\n"
        "  - name: dev\n"
        f"    project: {{ repo: {repo} }}\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "    channels: [ { type: fake, role: control, channel_id: 'dev-chat' } ]\n"
        f"{generic_block}",
        encoding="utf-8",
    )
    return loader.load(path)


def _open_request(conn, cfg, *, team="content", source="pm:content-approval-watch",
                  fingerprint="content-approval:slug:demo:draft:1", text=None):
    text = text or (
        "Daniel approved one content draft action in argus-content.\n"
        "Approved action: draft\n"
        "Run content desk pipeline as internal draft work only."
    )
    eid = events.ingest_signal(
        conn, cfg, team=team, source=source, fingerprint=fingerprint,
        payload={"text": text},
    )
    rid = pipeline.open_request(
        conn, cfg, event_id=eid, team_id=team, conversation_id=None,
        fingerprint=fingerprint,
    )
    conn.commit()
    return rid


def _age_request(conn, request_id, minutes=60):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE requests
            SET created_at=now() - make_interval(mins => %s),
                updated_at=now() - make_interval(mins => %s)
            WHERE id=%s
            """,
            (minutes, minutes, request_id),
        )
        cur.execute(
            """
            UPDATE jobs
            SET created_at=now() - make_interval(mins => %s),
                updated_at=now() - make_interval(mins => %s),
                heartbeat_at=NULL
            WHERE request_id=%s
            """,
            (minutes, minutes, request_id),
        )
    conn.commit()


def _run_watchdog(conn, cfg):
    inserted = completion_watchdog.run(conn, cfg)
    if inserted:
        executor.process_proposed(conn, cfg)
    conn.commit()
    return inserted


def test_old_content_approval_request_alerts_once(conn, tmp_path):
    fake.SENT.clear()
    cfg = _cfg(tmp_path)
    rid = _open_request(conn, cfg)
    _age_request(conn, rid)

    assert _run_watchdog(conn, cfg) == 1
    assert _run_watchdog(conn, cfg) == 0

    assert len(fake.SENT) == 1
    channel, text = fake.SENT[0]
    assert channel == "argus-content"
    assert f"Request: {rid}" in text
    assert "Fingerprint: content-approval:slug:demo:draft:1" in text
    assert "Source: pm:content-approval-watch" in text
    assert "Current stage/job:" in text
    assert f"Suggested retry: `argus request retry {rid}`" in text


def test_content_schedule_watchdog_coalesces_same_lineage(conn, tmp_path):
    fake.SENT.clear()
    cfg = _cfg(tmp_path)
    fingerprints = [
        "content-approval:pr:2:schedule:1783139496.457569",
        "content-approval:pr:2:schedule:1783139798.681899",
        "content-approval:pr:2:schedule:1783143407.541939",
        "content-approval:pr:2:schedule:1783134871.010009",
        "content-approval:pr:2:schedule:1783139495.990959",
        "content-approval:pr:2:schedule:1783139798.232729",
        "content-approval:pr:2:schedule:1783134871.445199",
        "content-approval:pr:2:schedule:1783142804.704149",
        "content-approval:pr:2:schedule:1783139797.427979",
        "content-approval:pr:2:schedule:1783139797.782729",
    ]
    request_ids = []
    for fingerprint in fingerprints:
        rid = _open_request(
            conn,
            cfg,
            fingerprint=fingerprint,
            text=(
                "Daniel approved one content live action in argus-content.\n"
                "Approved action: schedule\n"
                "Run content scheduling pipeline only for this exact target and action."
            ),
        )
        request_ids.append(rid)
        _age_request(conn, rid)

    assert _run_watchdog(conn, cfg) == 1
    assert _run_watchdog(conn, cfg) == 0
    assert len(fake.SENT) == 1
    assert "Fingerprint: content-approval:pr:2:schedule:" in fake.SENT[0][1]

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE requests SET status='failed', updated_at=now() WHERE id=%s",
            (request_ids[-1],),
        )
    conn.commit()

    assert _run_watchdog(conn, cfg) == 1
    assert len(fake.SENT) == 2
    assert "Reason: request or pipeline job failed" in fake.SENT[1][1]


def test_failed_content_approval_request_alerts(conn, tmp_path):
    fake.SENT.clear()
    cfg = _cfg(tmp_path)
    rid = _open_request(conn, cfg)
    job = jobs.claim(conn, "w1")
    conn.commit()
    jobs.finalize(
        conn,
        job.id,
        job.claim_token,
        status="failed",
        result={"error": "publisher guard failed"},
        run=RunRecord(role="developer", engine="echo", status="failed",
                      output="publisher guard failed"),
        actions=[],
    )
    conn.commit()

    assert _run_watchdog(conn, cfg) == 1

    assert len(fake.SENT) == 1
    assert "Reason: request or pipeline job failed" in fake.SENT[0][1]
    assert "Failure: publisher guard failed" in fake.SENT[0][1]


def test_done_request_ignored(conn, tmp_path):
    fake.SENT.clear()
    cfg = _cfg(tmp_path)
    rid = _open_request(conn, cfg)
    with conn.cursor() as cur:
        cur.execute("UPDATE requests SET status='done' WHERE id=%s", (rid,))
    conn.commit()
    _age_request(conn, rid)

    assert _run_watchdog(conn, cfg) == 0
    assert fake.SENT == []


def test_non_content_request_ignored_unless_generic_config_enabled(conn, tmp_path):
    fake.SENT.clear()
    cfg = _cfg(tmp_path)
    rid = _open_request(
        conn, cfg, team="dev", source="manual", fingerprint="manual:1",
        text="manual dev task",
    )
    _age_request(conn, rid)

    assert _run_watchdog(conn, cfg) == 0
    assert fake.SENT == []

    cfg = _cfg(tmp_path, generic=True)
    assert _run_watchdog(conn, cfg) == 1
    assert fake.SENT[0][0] == "dev-chat"


def test_draft_retryable_live_manual(conn, tmp_path):
    fake.SENT.clear()
    cfg = _cfg(tmp_path)
    draft = _open_request(conn, cfg, fingerprint="content-team-run:today:report:source")
    live = _open_request(
        conn,
        cfg,
        fingerprint="content-approval:slug:demo:publish:2",
        text=(
            "Daniel approved one content live action in argus-content.\n"
            "Approved action: publish\n"
            "Run content publishing pipeline only for this exact target and action."
        ),
    )
    _age_request(conn, draft)
    _age_request(conn, live)

    assert _run_watchdog(conn, cfg) == 2

    texts = [text for _channel, text in fake.SENT]
    draft_text = next(text for text in texts if f"Request: {draft}" in text)
    live_text = next(text for text in texts if f"Request: {live}" in text)
    assert f"Suggested retry: `argus request retry {draft}`" in draft_text
    assert f"Manual live retry only: `argus request retry {live} --force-live`" in live_text
    assert "Safety: watchdog only alerts." in live_text

    draft_retry = completion_watchdog.retry_request(conn, cfg, draft)
    live_retry = completion_watchdog.retry_request(conn, cfg, live)
    conn.rollback()
    assert draft_retry.ok is True
    assert live_retry.ok is False
    assert live_retry.status == "needs-force-live"
