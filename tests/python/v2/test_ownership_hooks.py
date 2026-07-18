import pytest
from psycopg.types.json import Json

from argus.v2.config import loader
from argus.v2.ingress import events
from argus.v2.orchestrator import pipeline
from argus.v2.ownership import hooks, store
from argus.v2.queue.models import Job


@pytest.fixture()
def cfg_ownership(tmp_path):
    config_path = tmp_path / "ownership.yaml"
    config_path.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n  - name: dev\n"
        "    ownership: { enabled: true }\n"
        "    project: { repo: /tmp/x, base_branch: main, test_cmd: 'true' }\n"
        "    roles: [ { name: developer, kind: builder, prompt: p },\n"
        "             { name: qa, kind: judge, prompt: p },\n"
        "             { name: senior, kind: judge, prompt: p } ]\n"
        "    pipeline: { stages: [developer, qa, senior], max_iters: 2 }\n"
    )
    return loader.load(config_path)


def _obligation_for_request(conn, request_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM team_obligations WHERE request_id=%s",
            (request_id,),
        )
        row = cur.fetchone()
    return store.get(conn, row[0]) if row else None


def _obligation_event_count(conn, obligation_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM team_obligation_events WHERE obligation_id=%s",
            (obligation_id,),
        )
        return cur.fetchone()[0]


def _open_request(conn, cfg, *, dedup_key="issue-1"):
    event_id = events.ingest_message(
        conn, cfg, team="dev", source="sentry",
        dedup_key=dedup_key, text="fix crash",
    )
    request_id = pipeline.open_request(
        conn, cfg, event_id=event_id, team_id="dev",
    )
    return event_id, request_id


def _senior_approved_request(conn, cfg, monkeypatch, *, dedup_key="approved"):
    event_id, request_id = _open_request(conn, cfg, dedup_key=dedup_key)
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM jobs WHERE request_id=%s", (request_id,))
        job_id = cur.fetchone()[0]
    monkeypatch.setattr(
        pipeline,
        "_pr_info",
        lambda *args, **kwargs: {
            "title": "Fix crash",
            "body": "Fix crash",
            "request": "fix crash",
            "summary_short": "Fixed crash",
            "checks": "QA: pass; Senior: approve",
            "risk_summary": "No review blockers detected.",
            "changed_files": ["app.py"],
        },
    )
    job = Job(
        id=job_id,
        request_id=request_id,
        event_id=event_id,
        conversation_id=None,
        team_id="dev",
        role="senior",
        stage=2,
        kind="pipeline",
        status="done",
        attempts=0,
        max_attempts=3,
        claim_token=None,
        exec_snapshot={},
        payload={},
    )
    return request_id, job


def test_enabled_team_signal_creates_and_links_obligation(conn, cfg_ownership):
    _event_id, request_id = _open_request(conn, cfg_ownership)

    item = _obligation_for_request(conn, request_id)

    assert item.fingerprint == "event:issue-1"
    assert item.status == "working"


def test_disabled_team_creates_no_obligation(conn, cfg_project):
    event_id = events.ingest_message(
        conn, cfg_project, team="dev", source="cli",
        dedup_key="plain-1", text="fix crash",
    )

    request_id = pipeline.open_request(
        conn, cfg_project, event_id=event_id,
        team_id="dev",
    )

    assert _obligation_for_request(conn, request_id) is None


def test_open_pr_does_not_complete_obligation(conn, cfg_ownership, monkeypatch):
    request_id, job = _senior_approved_request(conn, cfg_ownership, monkeypatch)

    pipeline._approve_done(conn, cfg_ownership, job)

    item = _obligation_for_request(conn, request_id)
    assert item.status == "awaiting_pr"
    assert item.completed_at is None
    assert item.action_id is not None


def test_existing_open_pr_action_is_linked_on_repeated_approval(
        conn, cfg_ownership, monkeypatch):
    request_id, job = _senior_approved_request(
        conn, cfg_ownership, monkeypatch, dedup_key="existing-pr",
    )
    pipeline._approve_done(conn, cfg_ownership, job)
    first = _obligation_for_request(conn, request_id)
    first_event_count = _obligation_event_count(conn, first.id)
    conn.commit()

    pipeline._approve_done(conn, cfg_ownership, job)

    second = _obligation_for_request(conn, request_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM actions WHERE request_id=%s AND type='open_pr'",
            (request_id,),
        )
        action_count = cur.fetchone()[0]
    assert second.action_id == first.action_id
    assert second.status == "awaiting_pr"
    assert action_count == 1
    assert _obligation_event_count(conn, second.id) == first_event_count


def test_review_failure_draft_pr_links_ownership(
        conn, cfg_ownership, monkeypatch):
    event_id, request_id = _open_request(
        conn, cfg_ownership, dedup_key="review-failure-draft",
    )
    project = cfg_ownership.team("dev").project
    project.autofix.force_draft_on_fail = True
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET result=%s WHERE request_id=%s AND role='developer'",
            (Json({"has_diff": True}), request_id),
        )
        cur.execute("SELECT id FROM jobs WHERE request_id=%s", (request_id,))
        job_id = cur.fetchone()[0]
    monkeypatch.setattr(
        pipeline,
        "_pr_info",
        lambda *args, **kwargs: {
            "title": "Fix crash",
            "body": "QA failed",
            "request": "fix crash",
            "summary_short": "Fixed crash",
            "checks": "QA: fail",
            "risk_summary": "QA failed",
            "changed_files": ["app.py"],
        },
    )
    job = Job(
        id=job_id,
        request_id=request_id,
        event_id=event_id,
        conversation_id=None,
        team_id="dev",
        role="qa",
        stage=1,
        kind="pipeline",
        status="done",
        attempts=0,
        max_attempts=3,
        claim_token=None,
        exec_snapshot={},
        payload={},
    )

    assert pipeline._open_draft_pr_after_failure(
        conn, cfg_ownership, job, "tests failed",
    ) is True

    item = _obligation_for_request(conn, request_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, payload->>'draft' FROM actions "
            "WHERE request_id=%s AND type='open_pr'",
            (request_id,),
        )
        action_id, draft = cur.fetchone()
    assert item.status == "awaiting_pr"
    assert item.action_id == action_id
    assert item.completed_at is None
    assert draft == "true"


def test_reopening_owned_event_reuses_nonterminal_request(
        conn, cfg_ownership, monkeypatch):
    request_id, job = _senior_approved_request(
        conn, cfg_ownership, monkeypatch, dedup_key="reopen-owned-event",
    )
    event_id = job.event_id
    pipeline._approve_done(conn, cfg_ownership, job)
    first = _obligation_for_request(conn, request_id)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM actions WHERE request_id=%s", (request_id,))
        first_action_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM jobs WHERE request_id=%s", (request_id,))
        first_job_count = cur.fetchone()[0]
    first_event_count = _obligation_event_count(conn, first.id)
    conn.commit()

    reopened_request_id = pipeline.open_request(
        conn, cfg_ownership, event_id=event_id, team_id="dev",
    )

    reopened = _obligation_for_request(conn, request_id)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM team_obligations")
        obligation_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM requests")
        request_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM actions WHERE request_id=%s", (request_id,))
        action_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM jobs WHERE request_id=%s", (request_id,))
        job_count = cur.fetchone()[0]

    assert reopened_request_id == request_id
    assert obligation_count == 1
    assert request_count == 1
    assert reopened.status == "awaiting_pr"
    assert reopened.action_id == first.action_id
    assert action_count == first_action_count
    assert job_count == first_job_count
    assert _obligation_event_count(conn, reopened.id) == first_event_count


def test_failure_blocks_obligation_with_classified_reason(conn, cfg_ownership):
    _event_id, request_id = _open_request(
        conn, cfg_ownership, dedup_key="blocked-request",
    )

    pipeline._fail(conn, cfg_ownership, request_id, "network access denied")

    item = _obligation_for_request(conn, request_id)
    assert item.status == "blocked"
    assert item.blocked_reason.startswith("Failure classification: environment blocker.")
    assert item.evidence["classification"] == "environment blocker"


def test_no_change_blocks_obligation_without_completion_proof(conn, cfg_ownership):
    _event_id, request_id = _open_request(
        conn, cfg_ownership, dedup_key="no-change",
    )

    pipeline._no_fix_close(
        conn, cfg_ownership, request_id, "No code change was warranted.",
    )

    item = _obligation_for_request(conn, request_id)
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests WHERE id=%s", (request_id,))
        request_status = cur.fetchone()[0]
    assert request_status == "done"
    assert item.status == "blocked"
    assert item.completed_at is None
    assert item.evidence["classification"] == "unverified resolution"


def test_hooks_are_no_ops_without_an_obligation(conn, cfg_ownership):
    missing = "00000000-0000-0000-0000-000000000000"

    assert hooks.on_request_working(
        conn, cfg_ownership, request_id=missing, team_id="dev",
    ) is None
    assert hooks.on_pr_proposed(
        conn, cfg_ownership, request_id=missing, action_id=missing, team_id="dev",
    ) is None
    assert hooks.on_request_blocked(
        conn, cfg_ownership, request_id=missing, team_id="dev",
        reason="Failure classification: unknown. Missing request.",
        classification="unknown",
    ) is None
