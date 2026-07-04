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


def test_live_alert_text_requires_readiness_checks():
    text = completion_watchdog._alert_text(
        completion_watchdog.WatchdogFinding(
            request_id="r1",
            team_id="content",
            category="slow",
            reason="request open for >= 45 minutes",
            source="pm:content-approval-watch",
            fingerprint="content-approval:slug:demo:publish:2",
            request_status="open",
            current_stage=0,
            job_id="j1",
            job_role="developer",
            job_stage=0,
            job_status="pending",
            failure_reason="",
            retry_command="argus request retry r1 --force-live",
            retryable=False,
            destination_ref="fake:argus-content",
        )
    )

    assert "Manual live retry only: `argus request retry r1 --force-live`" in text
    assert "Required live-readiness checks:" in text
    for check in (
        "approval proof",
        "durable media",
        "CTA routes",
        "DM activation",
        "Metricool targets",
        "connector auth",
    ):
        assert check in text


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
    assert "Required live-readiness checks:" in live_text
    for check in (
        "approval proof",
        "durable media",
        "CTA routes",
        "DM activation",
        "Metricool targets",
        "connector auth",
    ):
        assert check in live_text
    assert "Safety: watchdog only alerts." in live_text

    draft_retry = completion_watchdog.retry_request(conn, cfg, draft)
    live_retry = completion_watchdog.retry_request(conn, cfg, live)
    conn.rollback()
    assert draft_retry.ok is True
    assert live_retry.ok is False
    assert live_retry.status == "needs-force-live"
    assert "live-readiness check" in live_retry.reason
    assert "Metricool targets" in live_retry.reason
