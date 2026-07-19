from __future__ import annotations

import psycopg
import pytest
from psycopg.types.json import Jsonb

from argus.v2 import alerts
from argus.v2.config import loader
from argus.v2.ingress import events
from argus.v2.ownership import code, cycle, maintenance, store


@pytest.fixture()
def cfg_maintenance(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    path = tmp_path / "maintenance.yaml"
    path.write_text(
        "company:\n"
        "  name: c\n"
        "  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: dev\n"
        "    ownership:\n"
        "      enabled: true\n"
        "      max_active_obligations: 3\n"
        "      maintenance: { enabled: true, interval_hours: 24, max_open: 1 }\n"
        f"    project: {{ repo: {repo}, github_repo: acme/app, pm: {{ daily_limit: 3 }} }}\n"
        "    sources:\n"
        "      - { name: sentry-dev, type: sentry, config: { project: dev } }\n"
        "      - name: github-dev\n"
        "        type: github\n"
        "        config: { project: dev, repo: acme/app, labels: [argus] }\n"
        "    roles:\n"
        "      - { name: developer, kind: builder, prompt: p }\n"
        "      - { name: qa, kind: judge, prompt: p }\n"
        "      - { name: senior, kind: judge, prompt: p }\n"
        "    pipeline: { stages: [developer, qa, senior] }\n"
        "  - name: other\n"
        "    ownership:\n"
        "      enabled: true\n"
        "      maintenance: { enabled: true, interval_hours: 24, max_open: 1 }\n"
        f"    project: {{ repo: {repo}, pm: {{ daily_limit: 3 }} }}\n"
        "    sources:\n"
        "      - { name: sentry-other, type: sentry, config: { project: other } }\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n",
        encoding="utf-8",
    )
    return loader.load(path)


def _signal(
    conn,
    cfg,
    *,
    team="dev",
    source="sentry-dev",
    fingerprint="crash-abc",
    severity="error",
    message="Checkout crashes",
    **payload,
):
    return events.ingest_signal(
        conn,
        cfg,
        team=team,
        source=source,
        fingerprint=fingerprint,
        payload={
            "source": "github" if source.startswith("github") else "sentry",
            "severity": severity,
            "message": message,
            **payload,
        },
    )


def _maintenance_rows(conn, team="dev"):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fingerprint, kind, status, request_id, evidence "
            "FROM team_obligations WHERE team_id=%s ORDER BY created_at",
            (team,),
        )
        return cur.fetchall()


def test_quiet_team_with_no_evidence_dispatches_nothing(conn, cfg_maintenance):
    assert maintenance.collect_candidates(conn, cfg_maintenance, "dev") == []
    assert maintenance.dispatch_one(conn, cfg_maintenance, "dev", []) is None
    assert _maintenance_rows(conn) == []


def test_collects_only_configured_unresolved_evidence_and_ranks_deterministically(
    conn, cfg_maintenance
):
    _signal(
        conn,
        cfg_maintenance,
        fingerprint="warn-old",
        severity="warn",
        message="Slow query",
    )
    _signal(
        conn,
        cfg_maintenance,
        fingerprint="critical-z",
        severity="critical",
        message="Checkout down",
    )
    _signal(
        conn,
        cfg_maintenance,
        source="unconfigured",
        fingerprint="foreign-source",
        severity="critical",
    )
    _signal(
        conn,
        cfg_maintenance,
        fingerprint="resolved",
        severity="critical",
        state="resolved",
    )
    _signal(
        conn,
        cfg_maintenance,
        team="other",
        source="sentry-other",
        fingerprint="other-crash",
        severity="critical",
    )

    candidates = maintenance.collect_candidates(conn, cfg_maintenance, "dev")

    assert [item.title for item in candidates] == ["Checkout down", "Slow query"]
    assert candidates[0].fingerprint.endswith("critical-z")
    assert candidates[0].evidence["team_id"] == "dev"
    assert candidates[0].request_message == (
        "Investigate and fix this evidence-backed maintenance issue.\n\n"
        "Title: Checkout down\n"
        f"Fingerprint: {candidates[0].fingerprint}\n"
        "Severity: critical\n"
        "Source: connector event sentry-dev\n"
        "Evidence: Checkout down\n\n"
        "Stay within this evidence. Do not invent adjacent work."
    )


def test_collects_failed_request_draft_failure_alert_and_explicit_github_issue(
    conn, cfg_maintenance
):
    failed_event = _signal(
        conn,
        cfg_maintenance,
        fingerprint="failed-origin",
        severity="warn",
        message="Original work",
    )
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO requests (event_id, team_id, status, fingerprint) "
            "VALUES (%s, 'dev', 'failed', 'failed-request') RETURNING id",
            (failed_event,),
        )
        request_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO actions "
            "(request_id, team_id, type, risk, status, idempotency_key, "
            " provider_ref, payload) "
            "VALUES (%s, 'dev', 'open_pr', 'reversible_internal', 'done', "
            "        'draft-failure', 'https://github.com/acme/app/pull/7', %s)",
            (
                request_id,
                Jsonb(
                    {
                        "draft": True,
                        "title": "Partial checkout fix",
                        "risk_summary": "needs review: QA failed; tests failed",
                    }
                ),
            ),
        )
    _signal(
        conn,
        cfg_maintenance,
        source="github-dev",
        fingerprint="github-dev-12",
        severity="warn",
        message="open issue #12: Improve checkout",
        kind="issue",
        number=12,
        title="Improve checkout",
        url="https://github.com/acme/app/issues/12",
        state="open",
    )
    alerts.record(
        conn,
        severity="error",
        project="dev",
        fingerprint="connector-health",
        message="Sentry ingestion is failing",
        payload={"source": "sentry-dev", "status": "open"},
    )

    candidates = maintenance.collect_candidates(conn, cfg_maintenance, "dev")
    fingerprints = {item.fingerprint for item in candidates}

    assert f"request:{request_id}" in fingerprints
    assert any(value.startswith("draft-pr:") for value in fingerprints)
    assert "github:github-dev:12" in fingerprints
    assert "alert:sentry-dev:connector-health" in fingerprints


def test_stale_resolved_incomplete_and_foreign_evidence_is_ineligible(
    conn, cfg_maintenance
):
    stale_id = _signal(
        conn,
        cfg_maintenance,
        fingerprint="stale",
        severity="critical",
        message="Old crash",
    )
    _signal(
        conn,
        cfg_maintenance,
        source="github-dev",
        fingerprint="github-bad-url",
        severity="warn",
        message="issue",
        kind="issue",
        number=2,
        title="Bad",
        url="https://github.com/other/repo/issues/2",
    )
    alerts.record(
        conn,
        severity="critical",
        project="dev",
        fingerprint="no-source",
        message="Cannot prove connector origin",
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE events SET received_at=clock_timestamp() - interval '31 days' "
            "WHERE id=%s",
            (stale_id,),
        )

    assert maintenance.collect_candidates(conn, cfg_maintenance, "dev") == []


def test_foreign_draft_pr_provider_is_ineligible(conn, cfg_maintenance):
    event_id = _signal(
        conn,
        cfg_maintenance,
        fingerprint="foreign-draft-origin",
        message="Original task",
    )
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO requests (event_id, team_id, status, fingerprint) "
            "VALUES (%s, 'dev', 'done', 'foreign-draft') RETURNING id",
            (event_id,),
        )
        request_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO actions "
            "(request_id, team_id, type, risk, status, idempotency_key, "
            " provider_ref, payload) "
            "VALUES (%s, 'dev', 'open_pr', 'reversible_internal', 'done', "
            "        'foreign-draft', 'https://github.com/other/repo/pull/7', %s)",
            (
                request_id,
                Jsonb(
                    {
                        "draft": True,
                        "title": "Foreign partial fix",
                        "risk_summary": "needs review: QA failed",
                    }
                ),
            ),
        )

    assert maintenance.collect_candidates(conn, cfg_maintenance, "dev") == []


def test_dispatches_highest_priority_candidate_once_and_links_exact_evidence(
    conn, cfg_maintenance
):
    _signal(
        conn,
        cfg_maintenance,
        fingerprint="low",
        severity="warn",
        message="Clean warning",
    )
    _signal(
        conn,
        cfg_maintenance,
        fingerprint="high",
        severity="critical",
        message="Fix crash",
    )
    candidates = maintenance.collect_candidates(conn, cfg_maintenance, "dev")

    request_id = maintenance.dispatch_one(conn, cfg_maintenance, "dev", candidates)
    second = maintenance.dispatch_one(conn, cfg_maintenance, "dev", candidates)

    assert request_id
    assert second is None
    rows = _maintenance_rows(conn)
    assert len(rows) == 1
    fingerprint, kind, status, linked_request, evidence = rows[0]
    assert fingerprint == f"event:{candidates[0].fingerprint}"
    assert kind == "maintenance"
    assert status == "working"
    assert str(linked_request) == request_id
    assert evidence["candidate"] == candidates[0].evidence
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload->>'text', payload->'evidence' "
            "FROM events WHERE source='pm:ownership-maintenance'"
        )
        text, evidence_payload = cur.fetchone()
    assert text == candidates[0].request_message
    assert evidence_payload == candidates[0].evidence


def test_interval_and_max_open_gates_dispatch(conn, cfg_maintenance):
    _signal(conn, cfg_maintenance, fingerprint="one", message="First issue")
    candidates = maintenance.collect_candidates(conn, cfg_maintenance, "dev")
    assert maintenance.dispatch_one(conn, cfg_maintenance, "dev", candidates)

    _signal(
        conn,
        cfg_maintenance,
        fingerprint="two",
        severity="critical",
        message="Second issue",
    )
    later = maintenance.collect_candidates(conn, cfg_maintenance, "dev")
    assert maintenance.dispatch_one(conn, cfg_maintenance, "dev", later) is None

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE team_obligations SET status='done', completed_at=now(), "
            "created_at=now() - interval '25 hours' WHERE kind='maintenance'"
        )
    assert maintenance.dispatch_one(conn, cfg_maintenance, "dev", later)


def test_max_open_blocks_even_after_interval_elapsed(conn, cfg_maintenance):
    item = store.upsert(
        conn,
        team_id="dev",
        kind="maintenance",
        fingerprint="event:older-maintenance",
        title="Older maintenance",
        source_ref="alert:old",
        definition_of_done={"healthy": True},
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE team_obligations SET created_at=now() - interval '25 hours' "
            "WHERE id=%s",
            (item.id,),
        )
    _signal(
        conn,
        cfg_maintenance,
        fingerprint="new-after-interval",
        severity="critical",
        message="New issue",
    )
    candidates = maintenance.collect_candidates(conn, cfg_maintenance, "dev")

    assert maintenance.dispatch_one(conn, cfg_maintenance, "dev", candidates) is None
    assert len(_maintenance_rows(conn)) == 1


def test_daily_cap_reschedules_without_obligation_or_duplicate(conn, cfg_maintenance):
    team = cfg_maintenance.team("dev")
    team.project.pm.daily_limit = 1
    with conn.cursor() as cur:
        event_id = _signal(
            conn,
            cfg_maintenance,
            fingerprint="already-dispatched",
            message="Existing work",
        )
        cur.execute(
            "INSERT INTO requests (event_id, team_id, fingerprint) "
            "VALUES (%s, 'dev', 'already') RETURNING id",
            (event_id,),
        )
        existing_request = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO pm_dispatches (team_id, fingerprint, source, request_id) "
            "VALUES ('dev', 'already', 'pm', %s)",
            (existing_request,),
        )
    _signal(
        conn,
        cfg_maintenance,
        fingerprint="candidate",
        severity="critical",
        message="Candidate work",
    )
    candidates = maintenance.collect_candidates(conn, cfg_maintenance, "dev")

    assert maintenance.dispatch_one(conn, cfg_maintenance, "dev", candidates) is None
    assert _maintenance_rows(conn) == []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM events WHERE source='pm:ownership-maintenance'"
        )
        assert cur.fetchone()[0] == 0


def test_temporary_pipeline_unavailability_reschedules_cleanly(
    conn, cfg_maintenance, monkeypatch
):
    _signal(
        conn,
        cfg_maintenance,
        fingerprint="temporary",
        severity="critical",
        message="Retry this evidence later",
    )
    candidates = maintenance.collect_candidates(conn, cfg_maintenance, "dev")
    monkeypatch.setattr(
        maintenance.autofix,
        "dispatch",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("pipeline temporarily unavailable")
        ),
    )

    assert maintenance.dispatch_one(conn, cfg_maintenance, "dev", candidates) is None
    assert _maintenance_rows(conn) == []


def test_dispatch_rechecks_database_evidence_and_rejects_resolved_candidate(
    conn, cfg_maintenance
):
    event_id = _signal(
        conn,
        cfg_maintenance,
        fingerprint="resolved-later",
        severity="critical",
        message="Was broken",
    )
    candidates = maintenance.collect_candidates(conn, cfg_maintenance, "dev")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE events SET payload=payload || '{\"state\":\"resolved\"}'::jsonb "
            "WHERE id=%s",
            (event_id,),
        )

    assert maintenance.dispatch_one(conn, cfg_maintenance, "dev", candidates) is None
    assert _maintenance_rows(conn) == []


def test_concurrent_dispatch_is_idempotent(conn, pg_dsn, cfg_maintenance):
    _signal(
        conn,
        cfg_maintenance,
        fingerprint="concurrent",
        severity="critical",
        message="One repair",
    )
    conn.commit()
    with psycopg.connect(pg_dsn) as other:
        with other.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext('argus-owner:' || %s))",
                ("dev",),
            )
        candidates = maintenance.collect_candidates(conn, cfg_maintenance, "dev")
        assert maintenance.dispatch_one(conn, cfg_maintenance, "dev", candidates) is None
    candidates = maintenance.collect_candidates(conn, cfg_maintenance, "dev")
    assert maintenance.dispatch_one(conn, cfg_maintenance, "dev", candidates)


def test_cycle_dispatches_one_maintenance_candidate(conn, cfg_maintenance):
    _signal(
        conn,
        cfg_maintenance,
        fingerprint="cycle-candidate",
        severity="critical",
        message="Cycle should own this",
    )

    result = cycle.run(conn, cfg_maintenance, team_id="dev")

    assert result.teams == 1
    assert len(_maintenance_rows(conn)) == 1


def test_cycle_reconciles_due_maintenance_code_work(
    conn, cfg_maintenance, monkeypatch
):
    item = store.upsert(
        conn,
        team_id="dev",
        kind="maintenance",
        fingerprint="event:maintenance-pr",
        title="Maintain checkout",
        source_ref="event:evidence",
        definition_of_done={"healthy": True},
    )
    store.transition(conn, item.id, to_status="working", reason="work started")
    item = store.transition(
        conn, item.id, to_status="awaiting_pr", reason="draft proposed"
    )
    seen = []

    def fake_reconcile(_conn, _cfg, obligation, **_kwargs):
        seen.append(obligation)
        return code.ReconcileResult(str(obligation.id), obligation.status)

    monkeypatch.setattr(cycle.code, "reconcile", fake_reconcile)

    cycle.run(conn, cfg_maintenance, team_id="dev")

    assert [value.id for value in seen] == [item.id]
