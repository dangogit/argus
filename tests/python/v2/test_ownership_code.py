from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from psycopg.types.json import Jsonb

from argus.v2.config import loader
from argus.v2.ownership import code, store


SHA = "a" * 40
MERGE_SHA = "b" * 40
RUN_URL = "https://github.com/acme/luma/actions/runs/99"


@pytest.fixture()
def cfg_ownership(tmp_path):
    path = tmp_path / "ownership-code.yaml"
    path.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n  - name: dev\n"
        "    autonomy:\n"
        "      actions: { ready_pr: auto, merge_pr: auto }\n"
        "    ownership:\n"
        "      enabled: true\n"
        "      cycle_seconds: 60\n"
        "      max_attempts: 2\n"
        "      code:\n"
        "        auto_ready: true\n"
        "        auto_merge: true\n"
        "        allowed_base_branches: [staging]\n"
        "        required_checks: [test]\n"
        "        deploy_workflow: Deploy to Staging\n"
        "        live_url: https://staging.example.com\n"
        "        smoke_paths: [/, /health]\n"
        "        deployment_timeout_minutes: 30\n"
        "    project:\n"
        "      repo: /repo\n"
        "      github_repo: acme/luma\n"
        "      work_branch_prefix: argus\n"
        "    channels: [ { type: fake, role: control, channel_id: owner } ]\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n",
        encoding="utf-8",
    )
    return loader.load(path)


class Runner:
    def __init__(self, *, state="OPEN", draft=True, merge_sha="", deploy=None,
                 url="https://github.com/acme/luma/pull/42"):
        self.state = state
        self.draft = draft
        self.merge_sha = merge_sha
        self.deploy = deploy or []
        self.url = url
        self.calls = []

    def __call__(self, argv, cwd=None):
        self.calls.append((argv, cwd))
        if argv[:3] == ["gh", "pr", "view"]:
            return json.dumps({
                "number": 42,
                "url": self.url,
                "state": self.state,
                "isDraft": self.draft,
                "mergeStateStatus": "CLEAN",
                "baseRefName": "staging",
                "headRefName": "argus/req-1",
                "headRefOid": SHA,
                "mergeCommit": {"oid": self.merge_sha} if self.merge_sha else None,
                "files": [{"path": "src/app.py"}],
                "statusCheckRollup": [{
                    "name": "test",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                }],
            })
        if argv[:3] == ["gh", "run", "list"]:
            return json.dumps(self.deploy)
        raise AssertionError(f"unexpected command: {argv}")


@dataclass
class Response:
    status_code: int


def _item(conn, *, status="awaiting_merge", provider_ref="42", action_id=None):
    item = store.upsert(
        conn,
        team_id="dev",
        kind="code",
        fingerprint=f"code:{status}:{provider_ref}:{action_id}",
        title="Fix crash",
        source_ref="sentry:crash",
        definition_of_done={"pr": True},
    )
    store.transition(conn, item.id, to_status="working", reason="work started")
    if status == "awaiting_pr":
        item = store.transition(
            conn, item.id, to_status="awaiting_pr", reason="PR proposed")
    elif status == "awaiting_merge":
        item = store.transition(
            conn, item.id, to_status="awaiting_pr", reason="PR proposed")
        item = store.transition(
            conn, item.id, to_status="awaiting_merge", reason="PR opened")
    elif status == "awaiting_deploy":
        item = store.transition(
            conn, item.id, to_status="awaiting_pr", reason="PR proposed")
        item = store.transition(
            conn, item.id, to_status="awaiting_merge", reason="PR opened")
        item = store.transition(
            conn,
            item.id,
            to_status="awaiting_deploy",
            reason="PR merged",
            evidence={"merge_sha": MERGE_SHA},
        )
    elif status == "verifying":
        item = store.transition(
            conn, item.id, to_status="verifying", reason="deploy succeeded",
            evidence={"merge_sha": MERGE_SHA, "workflow_url": RUN_URL},
        )
    else:
        raise AssertionError(status)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE team_obligations SET provider_ref=%s, action_id=%s WHERE id=%s",
            (provider_ref, action_id, item.id),
        )
    return store.get(conn, item.id)


def _action(conn, *, action_type, status, provider_ref=None, error=None,
            key="linked-action"):
    payload = {"error": error} if error is not None else {}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO actions
              (team_id, type, risk, status, idempotency_key, provider_ref, payload)
            VALUES ('dev', %s, 'reversible_internal', %s, %s, %s, %s)
            RETURNING id
            """,
            (action_type, status, key, provider_ref, Jsonb(payload)),
        )
        return cur.fetchone()[0]


def _actions(conn, action_type):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, status, idempotency_key, payload, destination_ref "
            "FROM actions WHERE type=%s ORDER BY created_at, id",
            (action_type,),
        )
        return cur.fetchall()


def _event_statuses(conn, obligation_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT to_status FROM team_obligation_events "
            "WHERE obligation_id=%s ORDER BY id",
            (obligation_id,),
        )
        return [row[0] for row in cur.fetchall()]


def test_happy_path_records_only_canonical_lifecycle_transitions(
        conn, cfg_ownership):
    item = store.upsert(
        conn,
        team_id="dev",
        kind="code",
        fingerprint="code:happy-path",
        title="Fix crash",
        source_ref="sentry:happy",
        definition_of_done={"pr": True},
    )
    store.transition(conn, item.id, to_status="open", reason="obligation opened")
    store.transition(conn, item.id, to_status="working", reason="work started")
    open_action = _action(
        conn, action_type="open_pr", status="done", provider_ref="42",
        key="happy-open")
    store.link_action(conn, item.id, open_action)
    store.transition(conn, item.id, to_status="awaiting_pr", reason="PR proposed")

    runner = Runner(draft=True)
    code.reconcile(
        conn, cfg_ownership, store.get(conn, item.id), runner=runner,
        http_get=lambda *a, **k: None)
    code.reconcile(
        conn, cfg_ownership, store.get(conn, item.id), runner=runner,
        http_get=lambda *a, **k: None)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE actions SET status='done' WHERE id=%s",
            (store.get(conn, item.id).action_id,),
        )

    runner.draft = False
    code.reconcile(
        conn, cfg_ownership, store.get(conn, item.id), runner=runner,
        http_get=lambda *a, **k: None)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE actions SET status='done' WHERE id=%s",
            (store.get(conn, item.id).action_id,),
        )

    runner.state = "MERGED"
    runner.merge_sha = MERGE_SHA
    code.reconcile(
        conn, cfg_ownership, store.get(conn, item.id), runner=runner,
        http_get=lambda *a, **k: None)
    runner.deploy = [{
        "databaseId": 99,
        "status": "completed",
        "conclusion": "success",
        "url": RUN_URL,
        "headSha": MERGE_SHA,
    }]
    code.reconcile(
        conn, cfg_ownership, store.get(conn, item.id), runner=runner,
        http_get=lambda *a, **k: None)
    code.reconcile(
        conn, cfg_ownership, store.get(conn, item.id), runner=runner,
        http_get=lambda *a, **k: Response(200))

    assert _event_statuses(conn, item.id) == [
        "open", "working", "awaiting_pr", "awaiting_merge",
        "awaiting_deploy", "verifying", "done",
    ]


def test_open_pr_action_done_moves_to_awaiting_merge(conn, cfg_ownership):
    action_id = _action(
        conn,
        action_type="open_pr",
        status="done",
        provider_ref="https://github.com/acme/luma/pull/42",
    )
    item = _item(
        conn, status="awaiting_pr", provider_ref=None, action_id=action_id)

    result = code.reconcile(
        conn, cfg_ownership, item, runner=Runner(), http_get=lambda *a, **k: None)

    updated = store.get(conn, item.id)
    assert result.status == "awaiting_merge"
    assert updated.status == "awaiting_merge"
    assert updated.provider_ref == "42"


def test_open_pr_action_already_merged_still_records_merge_boundary(
        conn, cfg_ownership):
    action_id = _action(
        conn, action_type="open_pr", status="done", provider_ref="42")
    item = _item(
        conn, status="awaiting_pr", provider_ref=None, action_id=action_id)

    result = code.reconcile(
        conn, cfg_ownership, item,
        runner=Runner(state="MERGED", draft=False, merge_sha=MERGE_SHA),
        http_get=lambda *a, **k: None,
    )

    assert result.status == "awaiting_deploy"
    assert _event_statuses(conn, item.id)[-2:] == [
        "awaiting_merge", "awaiting_deploy"]


def test_open_pr_action_failure_blocks_with_canonical_error(conn, cfg_ownership):
    action_id = _action(
        conn, action_type="open_pr", status="failed", error="push rejected")
    item = _item(
        conn, status="awaiting_pr", provider_ref=None, action_id=action_id)

    code.reconcile(
        conn, cfg_ownership, item, runner=Runner(), http_get=lambda *a, **k: None)

    updated = store.get(conn, item.id)
    assert updated.status == "blocked"
    assert updated.blocked_reason == "open_pr action failed: push rejected"
    assert updated.evidence["action_error"] == "push rejected"


@pytest.mark.parametrize(
    "provider_ref",
    ["0", "01", "-1", "true", "https://github.com/acme/luma/pull/0",
     "https://user@example.com/acme/luma/pull/42",
     "https://github.com/acme/luma/pull/42#fragment",
     "https://bad_host/acme/luma/pull/42",
     "https://github.com/acme/%2Fetc/pull/42"],
)
def test_open_pr_provider_reference_must_be_canonical_positive_pr(
        conn, cfg_ownership, provider_ref):
    action_id = _action(
        conn, action_type="open_pr", status="done", provider_ref=provider_ref)
    item = _item(
        conn, status="awaiting_pr", provider_ref=None, action_id=action_id)

    code.reconcile(
        conn, cfg_ownership, item,
        runner=lambda *a, **k: pytest.fail("invalid PR ref must not be inspected"),
        http_get=lambda *a, **k: None,
    )

    assert store.get(conn, item.id).status == "blocked"


def test_inspected_pr_must_match_configured_repository(conn, cfg_ownership):
    item = _item(conn)

    result = code.reconcile(
        conn, cfg_ownership, item,
        runner=Runner(url="https://github.com/other/repo/pull/42"),
        http_get=lambda *a, **k: None,
    )

    assert result.blocked == 1
    assert store.get(conn, item.id).status == "blocked"
    assert _actions(conn, "ready_pr") == []


def test_safe_draft_queues_ready_action_once(conn, cfg_ownership):
    item = _item(conn)
    runner = Runner(draft=True)

    first = code.reconcile(
        conn, cfg_ownership, item, runner=runner, http_get=lambda *a, **k: None)
    second = code.reconcile(
        conn, cfg_ownership, store.get(conn, item.id),
        runner=runner, http_get=lambda *a, **k: None)

    actions = _actions(conn, "ready_pr")
    assert first.actions_proposed == 1
    assert second.actions_proposed == 0
    assert len(actions) == 1
    assert actions[0][2] == f"ready_pr:{item.id}:{SHA}"
    assert actions[0][3] == {
        "pr": 42,
        "cwd": "/repo",
        "expected_head_sha": SHA,
    }
    updated = store.get(conn, item.id)
    assert updated.action_id == actions[0][0]
    assert updated.evidence["policy"]["head_sha"] == SHA
    assert updated.evidence["policy"]["changed_files"] == ["src/app.py"]


def test_existing_failed_idempotent_action_blocks_on_relink(
        conn, cfg_ownership):
    item = _item(conn)
    action_id = _action(
        conn,
        action_type="ready_pr",
        status="failed",
        error="head changed",
        key=f"ready_pr:{item.id}:{SHA}",
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE actions SET payload=%s WHERE id=%s",
            (Jsonb({
                "pr": 42,
                "cwd": "/repo",
                "expected_head_sha": SHA,
                "error": "head changed",
            }), action_id),
        )

    result = code.reconcile(
        conn, cfg_ownership, item, runner=Runner(draft=True),
        http_get=lambda *a, **k: None,
    )

    updated = store.get(conn, item.id)
    assert result.blocked == 1
    assert updated.action_id == action_id
    assert updated.blocked_reason == "ready_pr action failed: head changed"


def test_ready_safe_pr_queues_merge_action_once(conn, cfg_ownership):
    ready_id = _action(
        conn, action_type="ready_pr", status="done", key="ready-done")
    item = _item(conn, action_id=ready_id)
    runner = Runner(draft=False)

    first = code.reconcile(
        conn, cfg_ownership, item, runner=runner, http_get=lambda *a, **k: None)
    second = code.reconcile(
        conn, cfg_ownership, store.get(conn, item.id),
        runner=runner, http_get=lambda *a, **k: None)

    actions = _actions(conn, "merge_pr")
    assert first.actions_proposed == 1
    assert second.actions_proposed == 0
    assert len(actions) == 1
    assert actions[0][2] == f"merge_pr:{item.id}:{SHA}"
    assert actions[0][3] == {
        "pr": 42,
        "cwd": "/repo",
        "expected_head_sha": SHA,
    }


def test_linked_action_awaiting_approval_stays_incomplete(
        conn, cfg_ownership):
    action_id = _action(
        conn, action_type="merge_pr", status="awaiting_approval",
        key="merge-awaiting-approval")
    item = _item(conn, action_id=action_id)

    result = code.reconcile(
        conn, cfg_ownership, item, runner=Runner(draft=False),
        http_get=lambda *a, **k: None)

    assert result.completed == 0
    assert result.actions_proposed == 0
    assert store.get(conn, item.id).status == "awaiting_merge"
    assert len(_actions(conn, "merge_pr")) == 1


def test_auto_ready_false_awaits_approval_and_notifies_once(
        conn, cfg_ownership):
    cfg_ownership.team("dev").ownership.code.auto_ready = False
    item = _item(conn)

    first = code.reconcile(
        conn, cfg_ownership, item, runner=Runner(draft=True),
        http_get=lambda *a, **k: None)
    second = code.reconcile(
        conn, cfg_ownership, store.get(conn, item.id), runner=Runner(draft=True),
        http_get=lambda *a, **k: None)

    assert first.status == second.status == "awaiting_approval"
    assert _actions(conn, "ready_pr") == []
    notifications = _actions(conn, "notify")
    assert len(notifications) == 1
    assert notifications[0][4] == "fake:owner"


def test_merged_pr_moves_to_awaiting_deploy(conn, cfg_ownership):
    merge_id = _action(
        conn, action_type="merge_pr", status="done", key="merge-done")
    item = _item(conn, action_id=merge_id)

    result = code.reconcile(
        conn, cfg_ownership, item,
        runner=Runner(state="MERGED", draft=False, merge_sha=MERGE_SHA),
        http_get=lambda *a, **k: None,
    )

    updated = store.get(conn, item.id)
    assert result.status == "awaiting_deploy"
    assert updated.status == "awaiting_deploy"
    assert updated.evidence["merge_sha"] == MERGE_SHA


def test_successful_workflow_moves_to_verifying(conn, cfg_ownership):
    item = _item(conn, status="awaiting_deploy")
    runner = Runner(deploy=[{
        "databaseId": 99,
        "status": "completed",
        "conclusion": "success",
        "url": RUN_URL,
        "headSha": MERGE_SHA,
    }])

    result = code.reconcile(
        conn, cfg_ownership, item, runner=runner, http_get=lambda *a, **k: None)

    updated = store.get(conn, item.id)
    assert result.status == "verifying"
    assert updated.status == "verifying"
    assert updated.evidence["workflow_url"] == RUN_URL
    assert [call for call in runner.calls if call[0][:3] == ["gh", "run", "list"]] == [
        ([
            "gh", "run", "list", "--workflow", "Deploy to Staging",
            "--commit", MERGE_SHA, "--json",
            "databaseId,status,conclusion,url,headSha", "--limit", "10",
        ], "/repo")
    ]


def test_2xx_smoke_completes_obligation(conn, cfg_ownership):
    item = _item(conn, status="verifying")
    calls = []

    def http_get(url, **kwargs):
        calls.append((url, kwargs))
        return Response(204 if url.endswith("/health") else 200)

    result = code.reconcile(
        conn, cfg_ownership, item, runner=Runner(), http_get=http_get)

    updated = store.get(conn, item.id)
    assert result.completed == 1
    assert updated.status == "done"
    assert updated.completed_at is not None
    assert calls == [
        ("https://staging.example.com/", {
            "follow_redirects": True, "timeout": 15}),
        ("https://staging.example.com/health", {
            "follow_redirects": True, "timeout": 15}),
    ]
    assert [row["status"] for row in updated.evidence["smoke"]] == [200, 204]
    assert all(row["workflow_url"] == RUN_URL for row in updated.evidence["smoke"])
    assert all(row["merge_sha"] == MERGE_SHA for row in updated.evidence["smoke"])
    assert all(row["checked_at"].endswith("+00:00") for row in updated.evidence["smoke"])


def test_failed_workflow_blocks_with_run_url(conn, cfg_ownership):
    item = _item(conn, status="awaiting_deploy")
    runner = Runner(deploy=[{
        "databaseId": 99,
        "status": "completed",
        "conclusion": "cancelled",
        "url": RUN_URL,
        "headSha": MERGE_SHA,
    }])

    result = code.reconcile(
        conn, cfg_ownership, item, runner=runner, http_get=lambda *a, **k: None)

    updated = store.get(conn, item.id)
    assert result.blocked == 1
    assert updated.status == "blocked"
    assert RUN_URL in updated.blocked_reason
    assert updated.evidence["workflow_url"] == RUN_URL


def test_deployment_timeout_uses_original_deploy_boundary(
        conn, cfg_ownership):
    item = _item(conn, status="awaiting_deploy")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE team_obligations SET evidence=evidence || %s WHERE id=%s",
            (Jsonb({"deploy_started_at": "2020-01-01T00:00:00+00:00"}), item.id),
        )
    item = store.get(conn, item.id)
    runner = Runner(deploy=[{
        "databaseId": 99,
        "status": "in_progress",
        "conclusion": "",
        "url": RUN_URL,
        "headSha": MERGE_SHA,
    }])

    result = code.reconcile(
        conn, cfg_ownership, item, runner=runner, http_get=lambda *a, **k: None)

    assert result.blocked == 1
    assert RUN_URL in store.get(conn, item.id).blocked_reason


def test_smoke_failure_retries_then_blocks(conn, cfg_ownership):
    item = _item(conn, status="verifying")

    def unavailable(*args, **kwargs):
        raise TimeoutError("staging timed out")

    first = code.reconcile(
        conn, cfg_ownership, item, runner=Runner(), http_get=unavailable)
    after_first = store.get(conn, item.id)
    second = code.reconcile(
        conn, cfg_ownership, after_first, runner=Runner(), http_get=unavailable)
    updated = store.get(conn, item.id)

    assert first.status == "verifying"
    assert after_first.attempts == 1
    assert second.blocked == 1
    assert updated.status == "blocked"
    assert updated.attempts == 2
    assert "staging timed out" in updated.blocked_reason


def test_smoke_requires_merge_and_workflow_proof_before_request(
        conn, cfg_ownership):
    item = _item(conn, status="verifying")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE team_obligations SET evidence='{}'::jsonb WHERE id=%s",
            (item.id,),
        )

    result = code.reconcile(
        conn, cfg_ownership, store.get(conn, item.id), runner=Runner(),
        http_get=lambda *a, **k: pytest.fail("unproven deploy must not be smoked"),
    )

    assert result.blocked == 1


@pytest.mark.parametrize(
    "live_url",
    ["http://staging.example.com", "https://user@staging.example.com",
     "https://staging.example.com/#fragment", "https:///missing-host",
     "https://staging.example.com/base/../admin"],
)
def test_smoke_rejects_unsafe_live_url_without_request(
        conn, cfg_ownership, live_url):
    cfg_ownership.team("dev").ownership.code.live_url = live_url
    item = _item(conn, status="verifying")

    result = code.reconcile(
        conn, cfg_ownership, item, runner=Runner(),
        http_get=lambda *a, **k: pytest.fail("unsafe URL must not be requested"),
    )

    assert result.blocked == 1
    assert store.get(conn, item.id).status == "blocked"
