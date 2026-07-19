from __future__ import annotations

import json
from dataclasses import replace

import psycopg
import pytest

from argus.v2 import cli
from argus.v2.config import loader
from argus.v2.ownership import store
from argus.v2.ownership.cycle import CycleResult


def _config(tmp_path, *, complete: bool = True):
    path = tmp_path / "owner.yaml"
    if complete:
        ownership = (
            "    autonomy:\n"
            "      actions: { ready_pr: approval, merge_pr: approval, support_reply: approval }\n"
            "    ownership:\n"
            "      enabled: true\n"
            "      code:\n"
            "        allowed_base_branches: [staging]\n"
            "        required_checks: [test]\n"
            "        deploy_workflow: Deploy to Staging\n"
            "        live_url: https://staging.example.test\n"
            "        smoke_paths: [/, /health]\n"
            "      support: { auto_send_low_risk: false, min_confidence: 0.92 }\n"
            "      maintenance: { enabled: true, interval_hours: 24, max_open: 1 }\n"
            "    project:\n"
            "      repo: /repo/dev\n"
            "      base_branch: staging\n"
            "      github_repo: acme/dev\n"
            "      pm: { daily_limit: 3 }\n"
            "    sources:\n"
            "      - name: dev-support\n"
            "        type: support_apps_script\n"
            "        secret_ref: '${env:OWNER_SUPPORT_KEY}'\n"
            "        config: { url: 'https://support.example.test/exec?key=do-not-log' }\n"
            "      - { name: dev-sentry, type: sentry, team: dev }\n"
            "    channels: [ { type: cli, role: control, channel_id: local } ]\n"
        )
    else:
        ownership = "    ownership: { enabled: true }\n"
    path.write_text(
        "company:\n"
        "  name: c\n"
        "  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: dev\n"
        f"{ownership}"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n",
        encoding="utf-8",
    )
    return path


def test_owner_subcommand_parsers_accept_exact_options():
    parser = cli.build_parser()

    cycle = parser.parse_args(["owner", "cycle", "--team", "dev", "--json"])
    listing = parser.parse_args([
        "owner", "list", "--team", "dev", "--status", "blocked",
        "--limit", "25", "--json",
    ])
    prove = parser.parse_args(["owner", "prove", "--team", "dev", "--json"])

    assert cycle.fn is cli.cmd_owner
    assert (cycle.owner_cmd, cycle.team, cycle.json) == ("cycle", "dev", True)
    assert listing.fn is cli.cmd_owner
    assert (listing.owner_cmd, listing.team, listing.status, listing.limit, listing.json) == (
        "list", "dev", "blocked", 25, True,
    )
    assert prove.fn is cli.cmd_owner
    assert (prove.owner_cmd, prove.team, prove.json) == ("prove", "dev", True)


@pytest.mark.parametrize("argv", [
    ["owner", "list", "--status", "unknown"],
    ["owner", "list", "--limit", "0"],
    ["owner", "list", "--limit", "501"],
])
def test_owner_list_parser_rejects_invalid_status_and_limit(argv):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(argv)

    assert exc.value.code == 2


class _CycleConn:
    autocommit = False

    def __init__(self):
        self.events: list[str] = []

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")

    def close(self):
        self.events.append("close")


def test_owner_cycle_runs_reconcile_then_actions_and_commits_once(
    tmp_path, monkeypatch, capsys
):
    path = _config(tmp_path)
    conn = _CycleConn()
    calls = []
    monkeypatch.setenv("OWNER_SUPPORT_KEY", "super-secret-owner-key")
    monkeypatch.setenv("ARGUS_CONFIG", str(path))
    monkeypatch.setattr(cli.pool, "connect", lambda: conn)
    monkeypatch.setattr(
        cli.ownership_cycle,
        "run",
        lambda passed, cfg, team_id=None: calls.append(("cycle", passed, team_id))
        or CycleResult(
            teams=1, reconciled=3, actions_proposed=1, completed=1,
            blocked=0, skipped_locked=0,
        ),
    )
    monkeypatch.setattr(
        cli.action_executor,
        "process_proposed",
        lambda passed, cfg, team_id=None: (
            calls.append(("actions", passed, team_id)) or 1
        ),
    )

    assert cli.main(["owner", "cycle", "--team", "dev", "--json"]) == 0

    assert [item[0] for item in calls] == ["cycle", "actions"]
    assert all(item[1] is conn for item in calls)
    assert calls[1][2] == "dev"
    assert conn.events == ["commit", "close"]
    assert json.loads(capsys.readouterr().out) == {
        "teams": 1,
        "reconciled": 3,
        "actions_proposed": 1,
        "completed": 1,
        "blocked": 0,
        "skipped_locked": 0,
    }


def test_owner_cycle_failure_rolls_back_without_partial_success(
    tmp_path, monkeypatch, capsys
):
    path = _config(tmp_path)
    conn = _CycleConn()
    monkeypatch.setenv("OWNER_SUPPORT_KEY", "secret")
    monkeypatch.setenv("ARGUS_CONFIG", str(path))
    monkeypatch.setattr(cli.pool, "connect", lambda: conn)
    monkeypatch.setattr(
        cli.ownership_cycle, "run", lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError(
                "reconciliation unavailable password=private "
                "https://provider.test/run?token=url-secret Bearer bearer-secret"
            )))

    assert cli.main(["owner", "cycle", "--json"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "owner cycle: reconciliation unavailable" in captured.err
    assert "private" not in captured.err
    assert "url-secret" not in captured.err
    assert "bearer-secret" not in captured.err
    assert conn.events == ["rollback", "close"]


def test_owner_list_json_is_deterministic_and_contains_only_summary_fields(
    conn, pg_dsn, tmp_path, monkeypatch, capsys
):
    path = _config(tmp_path)
    monkeypatch.setenv("OWNER_SUPPORT_KEY", "super-secret-owner-key")
    monkeypatch.setenv("ARGUS_CONFIG", str(path))
    first = store.upsert(
        conn, team_id="dev", kind="code", fingerprint="cli:first",
        title="Fix checkout", source_ref="sentry:one",
        definition_of_done={"secret": "definition-secret"},
    )
    second = store.upsert(
        conn, team_id="dev", kind="support", fingerprint="cli:second",
        title="Support request", source_ref="dev-support",
        definition_of_done={"provider_reply": True},
    )
    store.transition(
        conn, second.id, to_status="blocked", reason="manual review",
        evidence={"raw_thread": "private customer body", "token": "evidence-secret"},
    )
    conn.commit()

    assert cli.main([
        "owner", "list", "--team", "dev", "--limit", "10", "--json",
    ]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in payload["obligations"]] == sorted([
        str(first.id), str(second.id),
    ])
    assert set(payload["obligations"][0]) == {
        "id", "team", "kind", "status", "attempts", "next_check", "title",
    }
    text = json.dumps(payload)
    assert "private customer body" not in text
    assert "definition-secret" not in text
    assert "evidence-secret" not in text
    assert "super-secret-owner-key" not in text


def test_owner_list_filters_status_and_rejects_unknown_team(
    conn, pg_dsn, tmp_path, monkeypatch, capsys
):
    path = _config(tmp_path)
    monkeypatch.setenv("OWNER_SUPPORT_KEY", "secret")
    monkeypatch.setenv("ARGUS_CONFIG", str(path))
    first = store.upsert(
        conn, team_id="dev", kind="code", fingerprint="cli:open",
        title="Open", source_ref=None, definition_of_done={},
    )
    second = store.upsert(
        conn, team_id="dev", kind="code", fingerprint="cli:blocked",
        title="Blocked", source_ref=None, definition_of_done={},
    )
    store.transition(conn, second.id, to_status="blocked", reason="blocked")
    conn.commit()

    assert cli.main([
        "owner", "list", "--team", "dev", "--status", "blocked", "--json",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in payload["obligations"]] == [str(second.id)]
    assert str(first.id) not in json.dumps(payload)

    assert cli.main(["owner", "list", "--team", "missing", "--json"]) == 2
    assert "unknown team: missing" in capsys.readouterr().err


def test_owner_prove_reports_complete_policy_without_secrets_or_evidence(
    conn, pg_dsn, tmp_path, monkeypatch, capsys
):
    path = _config(tmp_path)
    monkeypatch.setenv("OWNER_SUPPORT_KEY", "super-secret-owner-key")
    monkeypatch.setenv("ARGUS_CONFIG", str(path))
    blocked = store.upsert(
        conn, team_id="dev", kind="support", fingerprint="cli:proof",
        title="Needs guidance", source_ref="dev-support",
        definition_of_done={"provider_reply": True},
    )
    store.transition(
        conn, blocked.id, to_status="blocked", reason="customer body private",
        evidence={"raw_thread": "private customer body", "reply": "private draft"},
    )
    conn.commit()

    assert cli.main(["owner", "prove", "--team", "dev", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["team"] == "dev"
    assert payload["work"] == {"blocked": 1, "due": 0}
    assert payload["actions"] == {
        "merge_pr": "approval", "ready_pr": "approval", "support_reply": "approval",
    }
    assert payload["code"]["base_branch"] == "staging"
    assert payload["code"]["allowed_base_branches"] == ["staging"]
    assert payload["code"]["required_checks"] == ["test"]
    assert payload["code"]["deploy_workflow"] == "Deploy to Staging"
    assert payload["code"]["live_smoke"] == {
        "url": "https://staging.example.test", "paths": ["/", "/health"],
    }
    assert payload["support"]["ready"] is True
    assert payload["maintenance"]["ready"] is True
    assert payload["maintenance"]["source_count"] == 2
    assert payload["missing_prerequisites"] == []
    text = json.dumps(payload)
    for forbidden in (
        "super-secret-owner-key", "do-not-log", "private customer body", "private draft",
        "OWNER_SUPPORT_KEY",
    ):
        assert forbidden not in text


def test_owner_prove_allows_code_only_team_with_vercel_deploy(
    conn, pg_dsn, tmp_path, monkeypatch, capsys
):
    path = tmp_path / "owner-code-only.yaml"
    path.write_text(
        "company:\n"
        "  name: c\n"
        "  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: tadam-agents\n"
        "    autonomy:\n"
        "      actions: { ready_pr: approval, merge_pr: approval, support_reply: approval }\n"
        "    ownership:\n"
        "      enabled: true\n"
        "      code:\n"
        "        allowed_base_branches: [staging]\n"
        "        required_checks: [CI]\n"
        "        deploy_provider: vercel\n"
        "        deploy_project: tadam-agents\n"
        "        deploy_scope: tadam-technology\n"
        "        deploy_vercel_auth: cli\n"
        "        live_url: https://tadam-agents-git-staging-tadam-technology.vercel.app\n"
        "      support: { enabled: false }\n"
        "    project:\n"
        "      repo: /repo/tadam-agents\n"
        "      base_branch: staging\n"
        "      github_repo: dangogit/tadam-agents\n"
        "    channels: [ { type: cli, role: control, channel_id: local } ]\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ARGUS_CONFIG", str(path))

    assert cli.main(["owner", "prove", "--team", "tadam-agents", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["missing_prerequisites"] == []
    assert payload["support"]["enabled"] is False
    assert payload["support"]["ready"] is False
    assert payload["code"]["deploy_provider"] == "vercel"
    assert payload["code"]["deploy_project"] == "tadam-agents"
    assert payload["code"]["deploy_scope"] == "tadam-technology"
    assert payload["code"]["deploy_vercel_auth"] == "cli"


def test_owner_prove_is_read_only(conn, pg_dsn, tmp_path, monkeypatch):
    path = _config(tmp_path)
    monkeypatch.setenv("OWNER_SUPPORT_KEY", "secret")
    monkeypatch.setenv("ARGUS_CONFIG", str(path))
    item = store.upsert(
        conn, team_id="dev", kind="code", fingerprint="cli:readonly",
        title="Read only", source_ref=None, definition_of_done={},
    )
    conn.commit()
    before = store.get(conn, item.id)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM team_obligation_events WHERE obligation_id=%s", (item.id,))
        event_count = cur.fetchone()[0]

    assert cli.main(["owner", "prove", "--team", "dev", "--json"]) == 0

    after = store.get(conn, item.id)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM team_obligation_events WHERE obligation_id=%s", (item.id,))
        after_event_count = cur.fetchone()[0]
    assert replace(before) == replace(after)
    assert event_count == after_event_count


def test_owner_prove_enabled_incomplete_policy_fails_with_all_missing_items(
    conn, pg_dsn, tmp_path, monkeypatch, capsys
):
    path = _config(tmp_path, complete=False)
    monkeypatch.setenv("ARGUS_CONFIG", str(path))

    assert cli.main(["owner", "prove", "--team", "dev", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert set(payload["missing_prerequisites"]) >= {
        "project",
        "allowed_base_branches",
        "required_checks",
        "deploy_workflow",
        "live_url",
        "action_override:ready_pr",
        "action_override:merge_pr",
        "action_override:support_reply",
        "support_source",
    }


def test_owner_commands_report_database_unavailable(
    tmp_path, monkeypatch, capsys
):
    path = _config(tmp_path)
    monkeypatch.setenv("OWNER_SUPPORT_KEY", "secret")
    monkeypatch.setenv("ARGUS_CONFIG", str(path))
    monkeypatch.setattr(
        cli.pool, "connect", lambda: (_ for _ in ()).throw(
            psycopg.OperationalError("database unavailable")))

    assert cli.main(["owner", "list", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "owner list: database unavailable" in captured.err


@pytest.mark.parametrize(("message", "forbidden"), [
    (
        "connect postgresql://admin:db-pass@db.example/argus failed",
        ("admin", "db-pass"),
    ),
    (
        "fetch https://robot:web-pass@example.test/path failed",
        ("robot", "web-pass"),
    ),
    (
        "redis://cache-user:cache-pass@cache.example/0 unavailable",
        ("cache-user", "cache-pass"),
    ),
    (
        "config secret: 'yaml secret value' api_key: api-secret "
        'token = "token secret value" password: pass-secret',
        ("yaml secret value", "api-secret", "token secret value", "pass-secret"),
    ),
])
def test_owner_error_redacts_url_userinfo_and_yaml_credentials(
    capsys, message, forbidden
):
    assert cli._owner_error("cycle", RuntimeError(message)) == 1

    error = capsys.readouterr().err
    for value in forbidden:
        assert value not in error
    assert "owner cycle:" in error


def test_owner_error_preserves_safe_noncredential_detail(capsys):
    message = "database unavailable: token expired before reconciliation"

    assert cli._owner_error("cycle", RuntimeError(message)) == 1

    assert message in capsys.readouterr().err
