import json
import subprocess
from pathlib import Path

import yaml

from argus.v2 import cli, opscheck
from argus.v2.config import loader
from argus.v2.onboarding import _commands


def _sample_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sample-app"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("agent rules\n", encoding="utf-8")
    (repo / "CLAUDE.md").write_text("claude rules\n", encoding="utf-8")
    (repo / "README.md").write_text("# Sample\n", encoding="utf-8")
    (repo / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run", "lint": "eslint ."}}),
        encoding="utf-8",
    )
    (repo / ".env").write_text(
        "SUPABASE_SERVICE_ROLE_KEY=do-not-copy-this-secret\n"
        "SENTRY_AUTH_TOKEN=also-secret\n"
        "NEXT_PUBLIC_POSTHOG_KEY=client-key-not-enough\n",
        encoding="utf-8",
    )
    (repo / ".env.example").write_text(
        "VERCEL_TOKEN=\nNEXT_PUBLIC_SUPABASE_URL=\n",
        encoding="utf-8",
    )
    (repo / "vercel.json").write_text("{}", encoding="utf-8")
    nested_vercel = repo / "apps" / "web" / ".vercel"
    nested_vercel.mkdir(parents=True)
    (nested_vercel / "project.json").write_text(
        json.dumps({
            "projectId": "prj_nested",
            "orgId": "team_nested",
            "projectName": "sample-web",
        }),
        encoding="utf-8",
    )
    (repo / "firebase.json").write_text("{}", encoding="utf-8")
    (repo / "supabase").mkdir()
    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: ci\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin",
                    "git@github.com:dangogit/sample-app.git"],
                   cwd=repo, check=True, capture_output=True)
    return repo


def test_onboard_project_generates_private_config_and_artifacts(tmp_path, capsys):
    repo = _sample_repo(tmp_path)
    config = tmp_path / "private" / "argus.yaml"
    out = tmp_path / "artifacts"

    rc = cli.main([
        "onboard", "project", str(repo),
        "--mode", "chat-only",
        "--config", str(config),
        "--out-dir", str(out),
        "--channel", "slack",
        "--channel-id", "C123",
        "--force",
    ])

    assert rc == 0
    assert "onboard: config=" in capsys.readouterr().out
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    team = data["teams"][0]
    roles = {role["name"]: role for role in team["roles"]}
    assert team["name"] == "sample-app"
    assert team["project"]["repo"] == str(repo.resolve())
    assert team["project"]["test_cmd"] == "npm test"
    assert team["project"]["github_repo"] == "dangogit/sample-app"
    assert roles["manager"]["engine"]["engine"] == "codex"
    assert roles["developer"]["engine"]["engine"] == "echo"
    assert team["channels"][0]["type"] == "slack"
    assert team["channels"][0]["channel_id"] == "C123"

    generated = "\n".join([
        config.read_text(encoding="utf-8"),
        (out / "argus.env.example.generated").read_text(encoding="utf-8"),
        (out / "argus.onboarding.md").read_text(encoding="utf-8"),
    ])
    assert "do-not-copy-this-secret" not in generated
    assert "also-secret" not in generated
    assert "client-key-not-enough" not in generated
    assert "SUPABASE_SERVICE_ROLE_KEY=" in generated
    assert "SENTRY_AUTH_TOKEN=" in generated
    assert "SENTRY_ORG=" in generated
    assert "SENTRY_PROJECT=" in generated
    assert "POSTHOG_PERSONAL_API_KEY=" in generated
    assert "POSTHOG_PROJECT_ID=" in generated
    assert "POSTHOG_HOST=" in generated
    assert "NEXT_PUBLIC_POSTHOG_KEY=" not in generated
    assert "A DSN is not enough" in generated
    assert "`NEXT_PUBLIC_POSTHOG_KEY` is not enough" in generated
    assert "--require-source-type posthog" in generated
    assert "--require-source-type sentry" in generated
    assert "--require-team-source-type sample-app:posthog" in generated
    assert "--require-team-source-type sample-app:sentry" in generated
    assert "--require-each-team-source-type posthog" in generated
    assert "--require-each-team-source-type sentry" in generated
    assert "AGENTS.md" in generated
    assert "CLAUDE.md" in generated
    assert "apps/web/.vercel/project.json: sample-web (team_nested)" in generated
    assert "`vercel`" in generated


def test_onboard_pm_mode_sets_safe_pr_defaults(tmp_path):
    repo = _sample_repo(tmp_path)
    config = tmp_path / "argus.yaml"

    assert cli.main([
        "onboard", "project", str(repo),
        "--mode", "pm-propose-pr",
        "--config", str(config),
        "--out-dir", str(tmp_path / "artifacts"),
        "--channel", "fake",
        "--manager-engine", "scripted",
        "--force",
    ]) == 0

    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    team = data["teams"][0]
    roles = {role["name"]: role for role in team["roles"]}
    assert team["pipeline"]["stages"] == ["developer", "qa", "senior"]
    assert roles["manager"]["engine"]["engine"] == "scripted"
    assert roles["developer"]["engine"]["engine"] == "codex"
    assert roles["qa"]["engine"]["engine"] == "codex"
    assert team["project"]["allow_code_mode"] is True
    assert team["project"]["allow_network"] is True
    assert team["project"]["autofix"]["draft"] is True
    assert team["project"]["pm"]["daily_limit"] == 1
    assert "ARGUS_RESULT" in roles["developer"]["prompt"]
    assert '"ready": true' in roles["developer"]["prompt"]
    assert "ARGUS_RESULT" in roles["qa"]["prompt"]
    assert '"verdict": "pass"' in roles["qa"]["prompt"]
    assert "QA-sensitive work cannot close" in roles["qa"]["prompt"]
    assert "transcript documents" in roles["qa"]["prompt"]
    assert "access path" in roles["qa"]["prompt"]
    assert "item disposition" in roles["qa"]["prompt"]
    assert "verification coverage" in roles["qa"]["prompt"]
    assert "unresolved follow-up condition" in roles["qa"]["prompt"]
    assert "code regression" in roles["qa"]["prompt"]
    assert "environment blocker" in roles["qa"]["prompt"]
    assert "expected cancellation" in roles["qa"]["prompt"]
    assert "stale status" in roles["qa"]["prompt"]
    assert "unknown" in roles["qa"]["prompt"]
    assert "ARGUS_RESULT" in roles["senior"]["prompt"]
    assert '"decision": "approve"' in roles["senior"]["prompt"]
    assert "QA-sensitive work cannot close" in roles["senior"]["prompt"]
    assert "transcript documents" in roles["senior"]["prompt"]
    assert "access path" in roles["senior"]["prompt"]
    assert "item disposition" in roles["senior"]["prompt"]
    assert "verification coverage" in roles["senior"]["prompt"]
    assert "unresolved follow-up condition" in roles["senior"]["prompt"]
    assert "failing PR summary" in roles["senior"]["prompt"]
    assert "code regression" in roles["senior"]["prompt"]
    assert "environment blocker" in roles["senior"]["prompt"]
    assert "expected cancellation" in roles["senior"]["prompt"]
    assert "stale status" in roles["senior"]["prompt"]
    assert "unknown" in roles["senior"]["prompt"]


def test_onboard_project_supports_whatsapp_channel(tmp_path):
    repo = _sample_repo(tmp_path)
    config = tmp_path / "argus.yaml"
    out = tmp_path / "artifacts"

    assert cli.main([
        "onboard", "project", str(repo),
        "--mode", "chat-only",
        "--config", str(config),
        "--out-dir", str(out),
        "--channel", "whatsapp",
        "--channel-id", "120363123@g.us",
        "--force",
    ]) == 0

    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    channel = data["teams"][0]["channels"][0]
    assert data["company"]["defaults"]["webhook_secret"] == "${env:ARGUS_WEBHOOK_SECRET}"
    assert channel["type"] == "whatsapp"
    assert channel["channel_id"] == "120363123@g.us"
    assert channel["secret_ref"] == "${env:ARGUS_WA_APIKEY}"
    assert channel["config"]["base_url"] == "${env:ARGUS_WA_URL}"
    assert channel["config"]["instance"] == "${env:ARGUS_WA_INSTANCE}"
    env = (out / "argus.env.example.generated").read_text(encoding="utf-8")
    assert "ARGUS_WEBHOOK_SECRET=" in env
    assert "ARGUS_WA_APIKEY=" in env
    assert "ARGUS_WA_INSTANCE=" in env
    assert "ARGUS_WA_URL=" in env


def test_wow_generates_code_enabled_pm_onboarding(tmp_path, monkeypatch, capsys):
    repo = _sample_repo(tmp_path)
    config = tmp_path / "private" / "argus.yaml"
    out = tmp_path / "wow"
    monkeypatch.delenv("ARGUS_DB_DSN", raising=False)
    monkeypatch.delenv("ARGUS_ENV_FILE", raising=False)
    monkeypatch.delenv("ARGUS_ENV_FILES", raising=False)

    rc = cli.main([
        "wow", str(repo),
        "--config", str(config),
        "--out-dir", str(out),
        "--channel", "fake",
        "--first-task", "tighten onboarding docs",
        "--force",
    ])

    assert rc == 0
    report = capsys.readouterr().out
    assert "ARGUS WOW" in report
    assert "mode: pm-propose-pr" in report
    assert "agent can code: true" in report
    assert "draft PR path: developer -> qa -> senior" in report
    assert "first task: not queued" in report
    assert "argus doctor --deep --live --json" in report
    assert "argus submit --team sample-app" in report
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    team = data["teams"][0]
    roles = {role["name"]: role for role in team["roles"]}
    assert team["project"]["allow_code_mode"] is True
    assert team["project"]["autofix"]["mode"] == "propose-pr"
    assert team["pipeline"]["stages"] == ["developer", "qa", "senior"]
    assert roles["developer"]["engine"]["engine"] == "codex"
    assert roles["qa"]["engine"]["engine"] == "codex"
    assert roles["senior"]["engine"]["engine"] == "codex"
    assert (out / "argus.env.example.generated").exists()
    assert (out / "argus.onboarding.md").exists()


def test_wow_queues_pm_request_when_db_ready(tmp_path, monkeypatch, pg_dsn, conn, capsys):
    repo = _sample_repo(tmp_path)
    config = tmp_path / "private" / "argus.yaml"
    out = tmp_path / "wow"
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)

    rc = cli.main([
        "wow", str(repo),
        "--config", str(config),
        "--out-dir", str(out),
        "--channel", "fake",
        "--first-task", "tighten onboarding docs",
        "--force",
    ])

    assert rc == 0
    report = capsys.readouterr().out
    assert "first task: queued PM request" in report
    assert "argus doctor --deep --live --json" in report
    assert "argus up --iterations 3" in report
    with conn.cursor() as cur:
        cur.execute("SELECT kind, source, dedup_key FROM events")
        assert cur.fetchall() == [("signal", "pm:wow", "wow:first-task:sample-app")]
        cur.execute("SELECT team_id, status, fingerprint FROM requests")
        assert cur.fetchall() == [("sample-app", "open", "wow:first-task:sample-app")]
        cur.execute("SELECT role, kind FROM jobs")
        assert cur.fetchall() == [("developer", "pipeline")]


def test_doctor_deep_reports_missing_engine_binary(tmp_path, monkeypatch, pg_dsn, capsys):
    repo = _sample_repo(tmp_path)
    config = tmp_path / "argus.yaml"
    config.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: dev\n"
        f"    project: {{ repo: {repo}, base_branch: main, test_cmd: 'true' }}\n"
        "    roles:\n"
        "      - { name: manager, kind: front, prompt: p, engine: { engine: codex } }\n"
        "      - { name: developer, kind: builder, prompt: p }\n"
        "    pipeline: { stages: [developer] }\n"
        "    channels: [ { type: fake, role: control, channel_id: local } ]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ARGUS_CONFIG", str(config))
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)
    monkeypatch.setenv("ARGUS_CODEX_BIN", "definitely-missing-codex")

    assert cli.main(["doctor", "--deep", "--json"]) == 1

    data = json.loads(capsys.readouterr().out)
    assert any(
        item["area"] == "engine"
        and item["name"] == "dev.manager"
        and item["status"] == "auth_failed"
        for item in data["checks"]
    )


def test_doctor_live_checks_github_token_with_api(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout='{"login":"argus"}', stderr="")

    monkeypatch.setattr(opscheck.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setenv("GH_TOKEN", "token")
    monkeypatch.setattr(opscheck.subprocess, "run", fake_run)

    assert opscheck._gh_check(live=True).__dict__ == {
        "area": "github",
        "name": "auth",
        "status": "ok",
        "detail": "gh api user",
    }
    assert calls == [["gh", "api", "user"]]


def test_doctor_live_checks_github_stored_auth_without_token(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="github.com\n")

    monkeypatch.setattr(opscheck.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(opscheck.subprocess, "run", fake_run)

    assert opscheck._gh_check(live=True).__dict__ == {
        "area": "github",
        "name": "auth",
        "status": "ok",
        "detail": "gh auth status",
    }
    assert calls == [["gh", "auth", "status"]]


def test_doctor_deep_reports_missing_secret_without_traceback(tmp_path, monkeypatch, pg_dsn, capsys):
    config = tmp_path / "argus.yaml"
    config.write_text(
        "company:\n"
        "  name: c\n"
        "  defaults:\n"
        "    engine: { engine: echo }\n"
        "    webhook_secret: \"${env:ARGUS_WEBHOOK_SECRET}\"\n"
        "teams:\n"
        "  - name: dev\n"
        "    roles: [ { name: manager, kind: front, prompt: p } ]\n"
        "    pipeline: { stages: [manager] }\n"
        "    channels:\n"
        "      - type: slack\n"
        "        role: control\n"
        "        channel_id: C123\n"
        "        secret_ref: \"${env:SLACK_BOT_TOKEN}\"\n"
        "        config: { signing_secret: \"${env:SLACK_SIGNING_SECRET}\" }\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ARGUS_CONFIG", str(config))
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)
    for key in ("ARGUS_WEBHOOK_SECRET", "SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET"):
        monkeypatch.delenv(key, raising=False)

    assert cli.main(["doctor", "--deep", "--json"]) == 1

    out = capsys.readouterr()
    data = json.loads(out.out)
    assert data["checks"][0]["status"] == "missing_secret"
    assert "Traceback" not in out.err


def _fake_runtime_ok(_serve_url):
    return [
        opscheck.Check("runtime", "serve", "ok", "test"),
        opscheck.Check("runtime", "serve_unit", "ok", "test"),
        opscheck.Check("runtime", "up", "ok", "test"),
    ]


def test_parse_launchctl_allows_clean_or_manual_restart_status():
    units = opscheck._parse_launchctl(
        "123\t0\tcom.argus.serve\n"
        "456\t-15\tcom.argus.up\n"
    )

    assert units["serve"] == (True, "running")
    assert units["up"] == (True, "running")


def test_parse_launchctl_blocks_running_unit_after_crash_status():
    units = opscheck._parse_launchctl(
        "123\t0\tcom.argus.serve\n"
        "456\t1\tcom.argus.up\n"
    )

    assert units["serve"] == (True, "running")
    assert units["up"] == (False, "running last_status=1")


def _fake_config(tmp_path: Path, *, sources: str = "") -> Path:
    config = tmp_path / "argus.yaml"
    config.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: dev\n"
        "    roles: [ { name: manager, kind: front, prompt: p },"
        " { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "    channels: [ { type: fake, role: control, channel_id: local } ]\n"
        f"{sources}",
        encoding="utf-8",
    )
    return config


def test_go_live_fails_when_up_is_not_running(tmp_path, monkeypatch, pg_dsn, capsys):
    config = _fake_config(tmp_path)
    monkeypatch.setenv("ARGUS_CONFIG", str(config))
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)
    monkeypatch.setattr(opscheck, "_runtime_checks", lambda serve_url: [
        opscheck.Check("runtime", "serve", "ok", "test"),
        opscheck.Check("runtime", "up", "blocked", "missing"),
    ])

    assert cli.main([
        "go-live", "--public-url", "https://argus.example.com/slack", "--json",
    ]) == 1

    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "blocked"
    assert any(item["name"] == "up" and item["status"] == "blocked"
               for item in data["checks"])


def test_slack_channel_smoke_posts_and_deletes(tmp_path, monkeypatch):
    config = tmp_path / "argus.yaml"
    config.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: dev\n"
        "    roles: [ { name: manager, kind: front, prompt: p } ]\n"
        "    pipeline: { stages: [manager] }\n"
        "    channels:\n"
        "      - type: slack\n"
        "        role: control\n"
        "        channel_id: C123\n"
        "        secret_ref: \"${env:SLACK_BOT_TOKEN}\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_post(url, **kwargs):
        calls.append((url, kwargs["json"]))
        if url.endswith("/chat.postMessage"):
            return Response({"ok": True, "ts": "123.456"})
        if url.endswith("/chat.delete"):
            return Response({"ok": True})
        raise AssertionError(url)

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)

    checks = opscheck._slack_channel_smoke_checks(loader.load(config))

    assert checks == [opscheck.Check("slack", "dev.C123", "ok", "post_delete")]
    assert [call[0].rsplit("/", 1)[-1] for call in calls] == [
        "chat.postMessage",
        "chat.delete",
    ]


def test_slack_channel_smoke_reports_post_failure(tmp_path, monkeypatch):
    config = tmp_path / "argus.yaml"
    config.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: dev\n"
        "    roles: [ { name: manager, kind: front, prompt: p } ]\n"
        "    pipeline: { stages: [manager] }\n"
        "    channels:\n"
        "      - type: slack\n"
        "        role: control\n"
        "        channel_id: C123\n"
        "        secret_ref: \"${env:SLACK_BOT_TOKEN}\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": False, "error": "not_in_channel"}

    def fake_post(_url, **_kwargs):
        return Response()

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)

    checks = opscheck._slack_channel_smoke_checks(loader.load(config))

    assert checks == [
        opscheck.Check("slack", "dev.C123", "auth_failed", "post not_in_channel")
    ]


def test_slack_scope_checks_report_missing_history_scope(tmp_path, monkeypatch):
    config = tmp_path / "argus.yaml"
    config.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: dev\n"
        "    roles: [ { name: manager, kind: front, prompt: p } ]\n"
        "    pipeline: { stages: [manager] }\n"
        "    channels:\n"
        "      - type: slack\n"
        "        role: control\n"
        "        channel_id: C123\n"
        "        secret_ref: \"${env:SLACK_BOT_TOKEN}\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": False, "error": "missing_scope"}

    def fake_post(_url, **_kwargs):
        return Response()

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)

    checks = opscheck._slack_scope_checks(loader.load(config))

    assert checks == [
        opscheck.Check(
            "slack", "dev.C123.history_scope", "auth_failed", "history missing_scope"
        )
    ]


def test_slack_scope_checks_pass_with_history_and_info_scope(tmp_path, monkeypatch):
    config = tmp_path / "argus.yaml"
    config.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: dev\n"
        "    roles: [ { name: manager, kind: front, prompt: p } ]\n"
        "    pipeline: { stages: [manager] }\n"
        "    channels:\n"
        "      - type: slack\n"
        "        role: control\n"
        "        channel_id: C123\n"
        "        secret_ref: \"${env:SLACK_BOT_TOKEN}\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_post(url, **kwargs):
        if url.endswith("/conversations.info"):
            assert kwargs.get("data") == {"channel": "C123"}
            assert "json" not in kwargs
            return Response({"ok": True, "channel": {"name": "dev"}})
        return Response({"ok": True, "messages": []})

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)

    checks = opscheck._slack_scope_checks(loader.load(config))

    assert checks == [
        opscheck.Check(
            "slack", "dev.C123.history_scope", "ok", "conversations.history"
        ),
        opscheck.Check(
            "slack", "dev.C123.info_scope", "ok", "conversations.info #dev"
        ),
    ]


def test_slack_scope_checks_skip_missing_info_scope(tmp_path, monkeypatch):
    config = tmp_path / "argus.yaml"
    config.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: dev\n"
        "    roles: [ { name: manager, kind: front, prompt: p } ]\n"
        "    pipeline: { stages: [manager] }\n"
        "    channels:\n"
        "      - type: slack\n"
        "        role: control\n"
        "        channel_id: C123\n"
        "        secret_ref: \"${env:SLACK_BOT_TOKEN}\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_post(url, **kwargs):
        if url.endswith("/conversations.info"):
            assert kwargs.get("data") == {"channel": "C123"}
            assert "json" not in kwargs
            return Response({"ok": False, "error": "missing_scope"})
        return Response({"ok": True, "messages": []})

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)

    checks = opscheck._slack_scope_checks(loader.load(config))

    assert checks == [
        opscheck.Check(
            "slack", "dev.C123.history_scope", "ok", "conversations.history"
        ),
        opscheck.Check(
            "slack", "dev.C123.info_scope", "advisory",
            "info missing_scope (add channels:read)"
        ),
    ]


def _slack_config(tmp_path: Path) -> Path:
    config = tmp_path / "argus.yaml"
    config.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: dev\n"
        "    roles: [ { name: manager, kind: front, prompt: p } ]\n"
        "    pipeline: { stages: [manager] }\n"
        "    channels:\n"
        "      - type: slack\n"
        "        role: control\n"
        "        channel_id: C123\n",
        encoding="utf-8",
    )
    return config


def test_channel_checks_block_duplicate_slack_channel_ids(tmp_path):
    config = tmp_path / "argus.yaml"
    config.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: dev\n"
        "    roles: [ { name: manager, kind: front, prompt: p } ]\n"
        "    pipeline: { stages: [manager] }\n"
        "    channels: [ { type: slack, role: control, channel_id: C123 } ]\n"
        "  - name: ops\n"
        "    roles: [ { name: manager, kind: front, prompt: p } ]\n"
        "    pipeline: { stages: [manager] }\n"
        "    channels: [ { type: slack, role: control, channel_id: C123 } ]\n",
        encoding="utf-8",
    )

    checks = opscheck._channel_checks(loader.load(config))

    assert opscheck.Check(
        "channel", "ops.slack", "blocked", "duplicate channel_id also used by dev"
    ) in checks


def test_slack_proof_accepts_last_known_events(tmp_path, conn):
    cfg = loader.load(_slack_config(tmp_path))
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO events
              (team_id, kind, source, payload, dedup_key, status, received_at)
            VALUES
              ('dev', 'message', 'slack:C123', '{"text":"old"}',
               'old-event', 'processed', now() - interval '2 hours')
            """
        )
        cur.execute(
            """
            INSERT INTO actions
              (team_id, type, risk, destination_ref, status, idempotency_key,
               updated_at)
            VALUES
              ('dev', 'reply', 'reversible_internal', 'slack:C123', 'done',
               'old-reply', now() - interval '2 hours')
            """
        )

    checks = opscheck._slack_proof_checks(conn, cfg)

    assert [check.status for check in checks] == ["ok", "ok"]
    assert "no event" not in checks[0].detail
    assert "no reply" not in checks[1].detail


def test_slack_proof_blocks_stale_when_fresh_required(tmp_path, conn):
    cfg = loader.load(_slack_config(tmp_path))
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO events
              (team_id, kind, source, payload, dedup_key, status, received_at)
            VALUES
              ('dev', 'message', 'slack:C123', '{"text":"old"}',
               'old-event-fresh', 'processed', now() - interval '2 hours')
            """
        )
        cur.execute(
            """
            INSERT INTO actions
              (team_id, type, risk, destination_ref, status, idempotency_key,
               updated_at)
            VALUES
              ('dev', 'reply', 'reversible_internal', 'slack:C123', 'done',
               'old-reply-fresh', now() - interval '2 hours')
            """
        )

    checks = opscheck._slack_proof_checks(conn, cfg, require_fresh=True)

    assert checks == [
        opscheck.Check(
            "slack", "dev.C123.event_received", "blocked", "no event in 30 minutes"
        ),
        opscheck.Check(
            "slack", "dev.C123.reply_sent", "blocked", "no reply in 30 minutes"
        ),
    ]


def test_slack_proof_blocks_never_seen_channel(tmp_path, conn):
    cfg = loader.load(_slack_config(tmp_path))

    checks = opscheck._slack_proof_checks(conn, cfg)

    assert checks == [
        opscheck.Check(
            "slack", "dev.C123.event_received", "blocked", "no event recorded"
        ),
        opscheck.Check(
            "slack", "dev.C123.reply_sent", "blocked", "no reply recorded"
        ),
    ]


def test_slack_proof_passes_recent_event_and_reply(tmp_path, conn):
    cfg = loader.load(_slack_config(tmp_path))
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO events
              (team_id, kind, source, payload, dedup_key, status, received_at)
            VALUES
              ('dev', 'message', 'slack:C123', '{"text":"fresh"}',
               'fresh-event', 'processed', now())
            """
        )
        cur.execute(
            """
            INSERT INTO actions
              (team_id, type, risk, destination_ref, status, idempotency_key,
               updated_at)
            VALUES
              ('dev', 'reply', 'reversible_internal', 'slack:C123', 'done',
               'fresh-reply', now())
            """
        )

    checks = opscheck._slack_proof_checks(conn, cfg)

    assert [check.status for check in checks] == ["ok", "ok"]
    assert [check.name for check in checks] == [
        "dev.C123.event_received",
        "dev.C123.reply_sent",
    ]


def test_go_live_blocks_slack_without_channel_proof(
    tmp_path, monkeypatch, pg_dsn, capsys
):
    config = _slack_config(tmp_path)
    monkeypatch.setenv("ARGUS_CONFIG", str(config))
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)
    monkeypatch.setattr(opscheck, "_runtime_checks", lambda serve_url: _fake_runtime_ok(serve_url))

    assert cli.main([
        "go-live", "--mode", "chat-only",
        "--public-url", "https://argus.example.com/slack", "--json",
    ]) == 1

    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "blocked"
    assert {
        "area": "slack",
        "name": "dev.C123.event_received",
        "status": "blocked",
        "detail": "no event recorded",
    } in data["checks"]
    assert {
        "area": "slack",
        "name": "dev.C123.reply_sent",
        "status": "blocked",
        "detail": "no reply recorded",
    } in data["checks"]


def test_go_live_passes_chat_only_with_fake_channel(tmp_path, monkeypatch, pg_dsn, capsys):
    config = _fake_config(tmp_path)
    monkeypatch.setenv("ARGUS_CONFIG", str(config))
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)
    monkeypatch.setattr(opscheck, "_runtime_checks", lambda serve_url: _fake_runtime_ok(serve_url))

    assert cli.main([
        "go-live", "--mode", "chat-only",
        "--public-url", "https://argus.example.com/slack", "--json",
    ]) == 0

    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "operational"


def test_go_live_passes_monitor_only_with_fake_connector(tmp_path, monkeypatch, pg_dsn, capsys):
    config = _fake_config(
        tmp_path,
        sources=(
            "    sources:\n"
            "      - { type: fake, name: fake-monitor,"
            " config: { signals: [ { fingerprint: one, payload: { severity: warn } } ] } }\n"
        ),
    )
    monkeypatch.setenv("ARGUS_CONFIG", str(config))
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)
    monkeypatch.setattr(opscheck, "_runtime_checks", lambda serve_url: _fake_runtime_ok(serve_url))

    assert cli.main([
        "go-live", "--mode", "monitor-only",
        "--public-url", "https://argus.example.com/slack", "--json",
    ]) == 0

    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "operational"
    assert any(item["area"] == "connector" and item["status"] == "ok"
               for item in data["checks"])


def test_go_live_blocks_missing_required_source_type(tmp_path, monkeypatch, pg_dsn, capsys):
    config = _fake_config(
        tmp_path,
        sources=(
            "    sources:\n"
            "      - { type: fake, name: fake-monitor,"
            " config: { signals: [ { fingerprint: one, payload: { severity: warn } } ] } }\n"
        ),
    )
    monkeypatch.setenv("ARGUS_CONFIG", str(config))
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)
    monkeypatch.setattr(opscheck, "_runtime_checks", lambda serve_url: _fake_runtime_ok(serve_url))

    assert cli.main([
        "go-live", "--mode", "monitor-only",
        "--public-url", "https://argus.example.com/slack",
        "--require-source-type", "sentry",
        "--json",
    ]) == 1

    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "blocked"
    assert {
        "area": "connector",
        "name": "required.sentry",
        "status": "blocked",
        "detail": "no source configured",
    } in data["checks"]


def test_doctor_deep_reports_required_source_type(tmp_path, monkeypatch, pg_dsn, capsys):
    config = _fake_config(
        tmp_path,
        sources=(
            "    sources:\n"
            "      - { type: fake, name: fake-monitor,"
            " config: { signals: [ { fingerprint: one, payload: { severity: warn } } ] } }\n"
        ),
    )
    monkeypatch.setenv("ARGUS_CONFIG", str(config))
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)

    assert cli.main([
        "doctor", "--deep",
        "--require-source-type", "fake",
        "--json",
    ]) == 0

    data = json.loads(capsys.readouterr().out)
    assert {
        "area": "connector",
        "name": "required.fake",
        "status": "ok",
        "detail": "configured=1",
    } in data["checks"]


def test_doctor_deep_accepts_support_apps_script_source(tmp_path, monkeypatch):
    config = tmp_path / "argus.yaml"
    config.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: dev\n"
        "    roles:\n"
        "      - { name: manager, kind: front, prompt: p }\n"
        "      - { name: support, kind: worker, prompt: p }\n"
        "    pipeline: { stages: [manager] }\n"
        "    channels: [ { type: fake, role: control, channel_id: local } ]\n"
        "    sources:\n"
        "      - type: support_apps_script\n"
        "        name: support-mail\n"
        "        secret_ref: \"${env:SUPPORT_KEY}\"\n"
        "        config: { url: 'https://support.test' }\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SUPPORT_KEY", "k")

    cfg = loader.load(config)
    checks = opscheck._connector_checks(cfg, live=False)
    data = {"checks": [item.__dict__ for item in checks]}
    assert {
        "area": "support",
        "name": "support-mail",
        "status": "ok",
        "detail": "support_apps_script:dev",
    } in data["checks"]
    assert not any(
        item["detail"] == "unknown type: support_apps_script"
        for item in data["checks"]
    )


def test_doctor_deep_live_checks_support_apps_script_transport(tmp_path, monkeypatch):
    config = tmp_path / "argus.yaml"
    config.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: dev\n"
        "    roles:\n"
        "      - { name: manager, kind: front, prompt: p }\n"
        "      - { name: support, kind: worker, prompt: p }\n"
        "    pipeline: { stages: [manager] }\n"
        "    channels: [ { type: fake, role: control, channel_id: local } ]\n"
        "    sources:\n"
        "      - type: support_apps_script\n"
        "        name: support-mail\n"
        "        secret_ref: \"${env:SUPPORT_KEY}\"\n"
        "        config: { url: 'https://support.test' }\n",
        encoding="utf-8",
    )

    class FakeTransport:
        def __init__(self, *, url, key, timeout=30):
            self.url = url
            self.key = key
            self.timeout = timeout

        def list_unread(self, limit):
            assert limit == 1
            return []

    monkeypatch.setenv("SUPPORT_KEY", "k")
    monkeypatch.setattr(
        "argus.v2.support.apps_script.AppsScriptTransport",
        FakeTransport,
    )

    cfg = loader.load(config)
    checks = opscheck._connector_checks(cfg, live=True)
    data = {"checks": [item.__dict__ for item in checks]}
    assert {
        "area": "support",
        "name": "support-mail",
        "status": "ok",
        "detail": "support_apps_script:dev",
    } in data["checks"]


def test_go_live_blocks_missing_required_team_source_type(
    tmp_path, monkeypatch, pg_dsn, capsys
):
    config = _fake_config(
        tmp_path,
        sources=(
            "    sources:\n"
            "      - { type: fake, name: fake-monitor,"
            " config: { signals: [ { fingerprint: one, payload: { severity: warn } } ] } }\n"
        ),
    )
    monkeypatch.setenv("ARGUS_CONFIG", str(config))
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)
    monkeypatch.setattr(opscheck, "_runtime_checks", lambda serve_url: _fake_runtime_ok(serve_url))

    assert cli.main([
        "go-live", "--mode", "monitor-only",
        "--public-url", "https://argus.example.com/slack",
        "--require-team-source-type", "dev:sentry",
        "--json",
    ]) == 1

    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "blocked"
    assert {
        "area": "connector",
        "name": "required_team.dev.sentry",
        "status": "blocked",
        "detail": "no source configured",
    } in data["checks"]


def test_go_live_blocks_missing_required_each_team_source_type(
    tmp_path, monkeypatch, pg_dsn, capsys
):
    config = _fake_config(
        tmp_path,
        sources=(
            "    sources:\n"
            "      - { type: fake, name: fake-monitor,"
            " config: { signals: [ { fingerprint: one, payload: { severity: warn } } ] } }\n"
        ),
    )
    monkeypatch.setenv("ARGUS_CONFIG", str(config))
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)
    monkeypatch.setattr(opscheck, "_runtime_checks", lambda serve_url: _fake_runtime_ok(serve_url))

    assert cli.main([
        "go-live", "--mode", "monitor-only",
        "--public-url", "https://argus.example.com/slack",
        "--require-each-team-source-type", "sentry",
        "--json",
    ]) == 1

    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "blocked"
    assert {
        "area": "connector",
        "name": "required_team.dev.sentry",
        "status": "blocked",
        "detail": "no source configured",
    } in data["checks"]


def test_doctor_deep_reports_required_team_source_type(
    tmp_path, monkeypatch, pg_dsn, capsys
):
    config = _fake_config(
        tmp_path,
        sources=(
            "    sources:\n"
            "      - { type: fake, name: fake-monitor,"
            " config: { signals: [ { fingerprint: one, payload: { severity: warn } } ] } }\n"
        ),
    )
    monkeypatch.setenv("ARGUS_CONFIG", str(config))
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)

    assert cli.main([
        "doctor", "--deep",
        "--require-team-source-type", "dev:fake",
        "--json",
    ]) == 0

    data = json.loads(capsys.readouterr().out)
    assert {
        "area": "connector",
        "name": "required_team.dev.fake",
        "status": "ok",
        "detail": "configured=1",
    } in data["checks"]


def test_doctor_deep_reports_required_each_team_source_type(
    tmp_path, monkeypatch, pg_dsn, capsys
):
    config = _fake_config(
        tmp_path,
        sources=(
            "    sources:\n"
            "      - { type: fake, name: fake-monitor,"
            " config: { signals: [ { fingerprint: one, payload: { severity: warn } } ] } }\n"
        ),
    )
    monkeypatch.setenv("ARGUS_CONFIG", str(config))
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)

    assert cli.main([
        "doctor", "--deep",
        "--require-each-team-source-type", "fake",
        "--json",
    ]) == 0

    data = json.loads(capsys.readouterr().out)
    assert {
        "area": "connector",
        "name": "required_team.dev.fake",
        "status": "ok",
        "detail": "configured=1",
    } in data["checks"]


def test_go_live_pm_mode_blocks_without_pr_smoke(tmp_path, monkeypatch, pg_dsn, capsys):
    config = tmp_path / "argus.yaml"
    repo = _sample_repo(tmp_path)
    config.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: scripted } }\n"
        "teams:\n"
        "  - name: dev\n"
        f"    project: {{ repo: {repo}, base_branch: main, test_cmd: 'true',"
        " autofix: { draft: true }, pm: { daily_limit: 1 } }\n"
        "    roles:\n"
        "      - { name: manager, kind: front, prompt: p }\n"
        "      - { name: developer, kind: builder, prompt: p }\n"
        "      - { name: qa, kind: judge, prompt: p }\n"
        "      - { name: senior, kind: judge, prompt: p }\n"
        "    pipeline: { stages: [developer, qa, senior] }\n"
        "    channels: [ { type: fake, role: control, channel_id: local } ]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ARGUS_CONFIG", str(config))
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)
    monkeypatch.setattr(opscheck, "_runtime_checks", lambda serve_url: _fake_runtime_ok(serve_url))

    assert cli.main([
        "go-live", "--mode", "pm-propose-pr",
        "--public-url", "https://argus.example.com/slack", "--json",
    ]) == 1

    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "blocked"
    assert any(item["area"] == "pm" and item["name"] == "pr_smoke"
               and item["status"] == "blocked" for item in data["checks"])


def test_go_live_pm_mode_passes_with_recent_pr_smoke(
    tmp_path, monkeypatch, pg_dsn, conn, capsys
):
    config = tmp_path / "argus.yaml"
    repo = _sample_repo(tmp_path)
    config.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: scripted } }\n"
        "teams:\n"
        "  - name: dev\n"
        f"    project: {{ repo: {repo}, base_branch: main, test_cmd: 'true',"
        " autofix: { draft: true }, pm: { daily_limit: 1 } }\n"
        "    roles:\n"
        "      - { name: manager, kind: front, prompt: p }\n"
        "      - { name: developer, kind: builder, prompt: p }\n"
        "      - { name: qa, kind: judge, prompt: p }\n"
        "      - { name: senior, kind: judge, prompt: p }\n"
        "    pipeline: { stages: [developer, qa, senior] }\n"
        "    channels: [ { type: fake, role: control, channel_id: local } ]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ARGUS_CONFIG", str(config))
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)
    monkeypatch.setattr(opscheck, "_runtime_checks", lambda serve_url: _fake_runtime_ok(serve_url))

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO events (team_id, kind, source, payload, dedup_key, status)
            VALUES ('dev', 'signal', 'fake:pm-smoke', '{}', 'pm-smoke', 'processed')
            RETURNING id
            """
        )
        event_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO requests (event_id, team_id, status, fingerprint)
            VALUES (%s, 'dev', 'done', 'pm-smoke')
            RETURNING id
            """,
            (event_id,),
        )
        request_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO actions
              (request_id, team_id, type, risk, idempotency_key, status, provider_ref)
            VALUES
              (%s, 'dev', 'open_pr', 'reversible_internal', 'open-pr-smoke',
               'done', 'https://github.com/dangogit/sample-app/pull/1')
            """,
            (request_id,),
        )
    conn.commit()

    assert cli.main([
        "go-live", "--mode", "pm-propose-pr",
        "--public-url", "https://argus.example.com/slack", "--json",
    ]) == 0

    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "operational"
    assert any(item["area"] == "pm" and item["name"] == "pr_smoke"
               and item["status"] == "ok" for item in data["checks"])


# _commands must pick the package manager from the lockfile: pnpm and yarn
# repos break under npm, and a lockfile-backed repo should install
# reproducibly (npm ci / frozen lockfile) rather than mutating the lock.

def _js_repo(tmp_path, lockfile=None, scripts=None):
    (tmp_path / "package.json").write_text(json.dumps(
        {"scripts": scripts if scripts is not None else {"test": "vitest"}}),
        encoding="utf-8")
    if lockfile:
        (tmp_path / lockfile).write_text("")
    return tmp_path


def test_pnpm_lockfile_gives_pnpm_commands(tmp_path):
    repo = _js_repo(tmp_path, "pnpm-lock.yaml")
    assert _commands(repo) == ("pnpm test", "pnpm install --frozen-lockfile")


def test_yarn_lockfile_gives_yarn_commands(tmp_path):
    repo = _js_repo(tmp_path, "yarn.lock")
    assert _commands(repo) == ("yarn test", "yarn install --frozen-lockfile")


def test_npm_lockfile_gives_npm_ci(tmp_path):
    repo = _js_repo(tmp_path, "package-lock.json")
    assert _commands(repo) == ("npm test", "npm ci")


def test_no_lockfile_falls_back_to_npm_install(tmp_path):
    repo = _js_repo(tmp_path)
    assert _commands(repo) == ("npm test", "npm install")


def test_lint_only_scripts_keep_runner(tmp_path):
    repo = _js_repo(tmp_path, "pnpm-lock.yaml", scripts={"lint": "eslint ."})
    assert _commands(repo) == ("pnpm run lint", "pnpm install --frozen-lockfile")
