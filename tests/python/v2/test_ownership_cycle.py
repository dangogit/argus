from __future__ import annotations

import json

import psycopg
import pytest
from psycopg.types.json import Jsonb

from argus.v2.config import loader
from argus.v2.ownership import cycle, store


SHA = "a" * 40


@pytest.fixture()
def cfg_cycle(tmp_path):
    path = tmp_path / "ownership-cycle.yaml"
    path.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: dev\n"
        "    ownership:\n"
        "      enabled: true\n"
        "      code:\n"
        "        auto_ready: true\n"
        "        allowed_base_branches: [staging]\n"
        "        required_checks: [test]\n"
        "    project:\n"
        "      repo: /repo\n"
        "      github_repo: acme/luma\n"
        "      work_branch_prefix: argus\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "  - name: disabled\n"
        "    ownership: { enabled: false }\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n",
        encoding="utf-8",
    )
    return loader.load(path)


def _awaiting_pr(conn, *, team_id="dev", fingerprint="cycle:one"):
    item = store.upsert(
        conn,
        team_id=team_id,
        kind="code",
        fingerprint=fingerprint,
        title="Fix crash",
        source_ref="sentry:crash",
        definition_of_done={"pr": True},
    )
    store.transition(conn, item.id, to_status="working", reason="work started")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO actions
              (team_id, type, risk, status, idempotency_key, provider_ref, payload)
            VALUES (%s, 'open_pr', 'reversible_internal', 'done', %s, '42', %s)
            RETURNING id
            """,
            (team_id, f"open:{fingerprint}", Jsonb({})),
        )
        action_id = cur.fetchone()[0]
    store.link_action(conn, item.id, action_id)
    return store.transition(
        conn, item.id, to_status="awaiting_pr", reason="PR proposed")


def _runner(argv, cwd=None):
    assert cwd == "/repo"
    return json.dumps({
        "number": 42,
        "url": "https://github.com/acme/luma/pull/42",
        "state": "OPEN",
        "isDraft": True,
        "mergeStateStatus": "CLEAN",
        "baseRefName": "staging",
        "headRefName": "argus/req-1",
        "headRefOid": SHA,
        "mergeCommit": None,
        "files": [{"path": "src/app.py"}],
        "statusCheckRollup": [{
            "name": "test", "status": "COMPLETED", "conclusion": "SUCCESS",
        }],
    })


def test_cycle_reconciles_due_code_obligation(conn, cfg_cycle):
    item = _awaiting_pr(conn)

    result = cycle.run(
        conn, cfg_cycle, runner=_runner, http_get=lambda *a, **k: None)

    assert result.teams == 1
    assert result.reconciled == 1
    assert result.skipped_locked == 0
    assert store.get(conn, item.id).status == "awaiting_merge"


def test_cycle_disabled_team_is_noop(conn, cfg_cycle):
    item = _awaiting_pr(
        conn, team_id="disabled", fingerprint="cycle:disabled")

    result = cycle.run(
        conn, cfg_cycle, team_id="disabled", runner=_runner,
        http_get=lambda *a, **k: None)

    assert result.teams == 0
    assert result.reconciled == 0
    assert result.skipped_locked == 0
    assert store.get(conn, item.id).status == "awaiting_pr"


def test_cycle_uses_transaction_advisory_lock_per_team(
        conn, pg_dsn, cfg_cycle):
    item = _awaiting_pr(conn)
    conn.commit()
    with psycopg.connect(pg_dsn) as lock_conn:
        with lock_conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext('argus-owner:' || %s))",
                ("dev",),
            )

        result = cycle.run(
            conn, cfg_cycle, team_id="dev", runner=_runner,
            http_get=lambda *a, **k: None)

        assert result.teams == 1
        assert result.reconciled == 0
        assert result.skipped_locked == 1
        assert store.get(conn, item.id).status == "awaiting_pr"


def test_cycle_filters_requested_team(conn, cfg_cycle):
    dev = _awaiting_pr(conn, fingerprint="cycle:dev")
    disabled = _awaiting_pr(
        conn, team_id="disabled", fingerprint="cycle:disabled-filtered")

    result = cycle.run(
        conn, cfg_cycle, team_id="dev", runner=_runner,
        http_get=lambda *a, **k: None)

    assert result.teams == 1
    assert result.reconciled == 1
    assert store.get(conn, dev.id).status == "awaiting_merge"
    assert store.get(conn, disabled.id).status == "awaiting_pr"


def test_cycle_nonreconcilable_rows_cannot_starve_due_work(conn, cfg_cycle):
    cfg_cycle.team("dev").ownership.max_active_obligations = 1
    waiting = _awaiting_pr(conn, fingerprint="cycle:waiting")
    distractors = []
    for index, status in enumerate(("open", "working", "blocked")):
        item = store.upsert(
            conn,
            team_id="dev",
            kind="code",
            fingerprint=f"cycle:distractor:{status}",
            title=f"Distractor {status}",
            source_ref=f"test:{index}",
            definition_of_done={"pr": True},
        )
        if status in {"working", "blocked"}:
            item = store.transition(
                conn, item.id, to_status="working", reason="started")
        if status == "blocked":
            item = store.transition(
                conn, item.id, to_status="blocked", reason="not actionable")
        distractors.append(item)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE team_obligations SET priority=100 "
            "WHERE id = ANY(%s)",
            ([item.id for item in distractors],),
        )

    result = cycle.run(
        conn, cfg_cycle, runner=_runner, http_get=lambda *a, **k: None)

    assert result.reconciled == 1
    assert store.get(conn, waiting.id).status == "awaiting_merge"


def test_cycle_requires_transactional_connection(pg_dsn, cfg_cycle):
    with psycopg.connect(pg_dsn, autocommit=True) as autocommit_conn:
        with pytest.raises(ValueError, match="autocommit=False"):
            cycle.run(
                autocommit_conn, cfg_cycle, runner=_runner,
                http_get=lambda *a, **k: None,
            )
