from pathlib import Path

import pytest

from argus.v2.config import loader

FIX = Path(__file__).parent / "fixtures" / "argus.yaml"


def test_webhook_secret_resolves_env_ref(tmp_path, monkeypatch):
    monkeypatch.setenv("WH", "topsecret")
    y = tmp_path / "a.yaml"
    y.write_text(
        'company:\n  name: c\n  defaults: { engine: { engine: echo }, '
        'webhook_secret: "${env:WH}" }\n'
        "teams:\n  - name: dev\n    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n", encoding="utf-8")
    cfg = loader.load(y)
    assert cfg.company.defaults.webhook_secret == "topsecret"  # resolved, not literal


def test_webhook_secret_literal_passes_through(tmp_path):
    y = tmp_path / "a.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo }, webhook_secret: plainval }\n"
        "teams:\n  - name: dev\n    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n", encoding="utf-8")
    assert loader.load(y).company.defaults.webhook_secret == "plainval"


def test_load_parses_team_and_roles():
    cfg = loader.load(FIX)
    team = cfg.team("dev")
    assert [r.name for r in team.roles] == ["manager", "developer", "qa", "senior"]
    assert team.pipeline.stages == ["developer", "qa", "senior"]


def test_project_pm_and_autofix_config(tmp_path):
    yaml = tmp_path / "a.yaml"
    yaml.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: t\n"
        "    project:\n"
        "      repo: /tmp/repo\n"
        "      base_branch: dev\n"
        "      autofix: { mode: propose-pr, draft: true }\n"
        "      pm: { daily_limit: 7, max_rework_attempts: 3 }\n"
        "    roles: [ { name: r, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [r] }\n",
        encoding="utf-8",
    )
    cfg = loader.load(yaml)
    project = cfg.team("t").project
    assert project.base_branch == "dev"
    assert project.autofix.draft is True
    assert project.pm.daily_limit == 7
    assert project.pm.max_rework_attempts == 3
    assert cfg.team("t").pipeline.max_iters == 3


def test_company_project_defaults_merge_into_team_project(tmp_path):
    yaml = tmp_path / "a.yaml"
    yaml.write_text(
        "company:\n"
        "  name: c\n"
        "  defaults:\n"
        "    engine: { engine: echo }\n"
        "    project:\n"
        "      base_branch: main\n"
        "      work_branch_prefix: argus/default\n"
        "      test_cmd: pytest -q\n"
        "      test_timeout_seconds: 123\n"
        "      autofix: { mode: propose-pr, draft: false, force_draft_on_fail: false }\n"
        "      pm: { daily_limit: 9 }\n"
        "teams:\n"
        "  - name: t\n"
        "    project:\n"
        "      repo: /tmp/repo\n"
        "      work_branch_prefix: argus/team\n"
        "    roles: [ { name: r, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [r] }\n",
        encoding="utf-8",
    )
    project = loader.load(yaml).team("t").project
    assert project.base_branch == "main"
    assert project.work_branch_prefix == "argus/team"
    assert project.test_cmd == "pytest -q"
    assert project.test_timeout_seconds == 123
    assert project.autofix.mode == "propose-pr"
    assert project.autofix.force_draft_on_fail is False
    assert project.pm.daily_limit == 9


def test_company_pipeline_default_fills_team_pipeline(tmp_path):
    yaml = tmp_path / "a.yaml"
    yaml.write_text(
        "company:\n"
        "  name: c\n"
        "  defaults:\n"
        "    engine: { engine: echo }\n"
        "    pipeline: { stages: [r], max_iters: 4 }\n"
        "teams:\n"
        "  - name: t\n"
        "    roles: [ { name: r, kind: builder, prompt: p } ]\n",
        encoding="utf-8",
    )
    cfg = loader.load(yaml)
    assert cfg.team("t").pipeline.stages == ["r"]
    assert cfg.team("t").pipeline.max_iters == 4


def test_missing_pipeline_without_default_fails(tmp_path):
    yaml = tmp_path / "a.yaml"
    yaml.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: t\n"
        "    roles: [ { name: r, kind: builder, prompt: p } ]\n",
        encoding="utf-8",
    )
    with pytest.raises(loader.ConfigError, match="missing pipeline"):
        loader.load(yaml)


def test_company_support_defaults_merge_into_support_source(tmp_path):
    yaml = tmp_path / "a.yaml"
    yaml.write_text(
        "company:\n"
        "  name: c\n"
        "  defaults:\n"
        "    engine: { engine: echo }\n"
        "    support:\n"
        "      mode: propose\n"
        "      daily_limit: 10\n"
        "teams:\n"
        "  - name: t\n"
        "    sources:\n"
        "    - type: support_apps_script\n"
        "      name: support\n"
        "      config: { url: 'https://support.test', daily_limit: 3 }\n"
        "    roles: [ { name: r, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [r] }\n",
        encoding="utf-8",
    )
    source = loader.load(yaml).team("t").sources[0]
    assert source.config["mode"] == "propose"
    assert source.config["daily_limit"] == 3


def test_engine_cascade_company_to_role(tmp_path):
    yaml = tmp_path / "a.yaml"
    yaml.write_text(
        "company:\n  name: c\n  defaults:\n    engine: { engine: echo }\n"
        "teams:\n  - name: t\n    engine: { engine: codex }\n"
        "    roles:\n      - { name: r1, kind: builder, prompt: p }\n"
        "      - { name: r2, kind: builder, prompt: p, engine: { engine: claude-code } }\n"
        "    pipeline: { stages: [r1] }\n"
    )
    cfg = loader.load(yaml)
    assert loader.resolve_engine(cfg, "t", "r1").engine == "codex"   # team override
    assert loader.resolve_engine(cfg, "t", "r2").engine == "claude-code"  # role override


def test_secret_reference_resolves(monkeypatch, tmp_path):
    monkeypatch.setenv("MY_TOKEN", "s3cret")
    yaml = tmp_path / "a.yaml"
    yaml.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "  sources:\n    - { type: sentry, name: s, scope: company, secret_ref: '${env:MY_TOKEN}' }\n"
        "teams:\n  - name: t\n    roles: [ { name: r, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [r] }\n"
    )
    cfg = loader.load(yaml)
    assert cfg.company.sources[0].secret == "s3cret"


def test_unsupported_channel_type_fails_validation(tmp_path):
    yaml = tmp_path / "a.yaml"
    yaml.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: t\n"
        "    roles: [ { name: r, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [r] }\n"
        "    channels: [ { type: discord, role: control, channel_id: C123 } ]\n",
        encoding="utf-8",
    )

    with pytest.raises(loader.ConfigError, match="unsupported channel type 'discord'"):
        loader.load(yaml)


def test_slack_channel_type_is_supported(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "signing-test")
    yaml = tmp_path / "a.yaml"
    yaml.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: t\n"
        "    roles: [ { name: r, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [r] }\n"
        "    channels:\n"
        "      - type: slack\n"
        "        role: control\n"
        "        channel_id: C123\n"
        "        secret_ref: '${env:SLACK_BOT_TOKEN}'\n"
        "        config:\n"
        "          signing_secret: '${env:SLACK_SIGNING_SECRET}'\n",
        encoding="utf-8",
    )

    channel = loader.load(yaml).team("t").channels[0]

    assert channel.type == "slack"
    assert channel.secret == "xoxb-test"
    assert channel.config["signing_secret"] == "signing-test"


def test_cli_channel_type_stays_valid(tmp_path):
    yaml = tmp_path / "a.yaml"
    yaml.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: t\n"
        "    roles: [ { name: r, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [r] }\n"
        "    channels: [ { type: cli, role: control, channel_id: local } ]\n",
        encoding="utf-8",
    )

    assert loader.load(yaml).team("t").channels[0].type == "cli"


def test_config_env_reference_resolves(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPABASE_URL", "https://demo.supabase.co")
    yaml = tmp_path / "a.yaml"
    yaml.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "  sources:\n"
        "    - type: supabase\n      name: sb\n      scope: company\n"
        "      team: t\n      config: { url: '${env:SUPABASE_URL}', nested: { v: '${env:SUPABASE_URL}' } }\n"
        "teams:\n  - name: t\n    roles: [ { name: r, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [r] }\n",
        encoding="utf-8",
    )
    cfg = loader.load(yaml)
    assert cfg.company.sources[0].config["url"] == "https://demo.supabase.co"
    assert cfg.company.sources[0].config["nested"]["v"] == "https://demo.supabase.co"


def test_missing_config_env_reference_fails(tmp_path):
    yaml = tmp_path / "a.yaml"
    yaml.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "  sources:\n"
        "    - type: supabase\n      name: sb\n      scope: company\n"
        "      team: t\n      config: { url: '${env:MISSING_SUPABASE_URL}' }\n"
        "teams:\n  - name: t\n    roles: [ { name: r, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [r] }\n",
        encoding="utf-8",
    )
    with pytest.raises(loader.ConfigError, match="MISSING_SUPABASE_URL"):
        loader.load(yaml)


def test_vision_role_cannot_use_hermes(tmp_path):
    yaml = tmp_path / "a.yaml"
    yaml.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: hermes } }\n"
        "teams:\n  - name: t\n"
        "    roles: [ { name: r, kind: builder, prompt: p, vision: true } ]\n"
        "    pipeline: { stages: [r] }\n"
    )
    with pytest.raises(loader.ConfigError, match="vision"):
        loader.load(yaml)
