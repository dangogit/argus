from pathlib import Path

import pytest
from pydantic import ValidationError

from argus.v2.config import loader, schema

FIX = Path(__file__).parent / "fixtures" / "argus.yaml"


def _load_yaml(tmp_path, contents):
    path = tmp_path / "argus.yaml"
    path.write_text(contents, encoding="utf-8")
    return loader.load(path)


def _validate_ownership(ownership):
    return loader.Config.model_validate({
        "company": {"name": "c", "defaults": {"engine": {"engine": "echo"}}},
        "teams": [{
            "name": "t",
            "roles": [{"name": "r", "kind": "builder", "prompt": "p"}],
            "pipeline": {"stages": ["r"]},
            "ownership": ownership,
        }],
    }).team("t").ownership


def test_ownership_defaults_are_disabled():
    team = loader.load(FIX).team("dev")

    assert team.ownership.enabled is False
    assert team.ownership.code.auto_ready is False
    assert team.ownership.code.auto_merge is False
    assert team.ownership.code.deploy_provider == "github"
    assert team.ownership.support.enabled is True
    assert team.ownership.support.auto_send_low_risk is False
    assert team.ownership.maintenance.enabled is False


def test_action_autonomy_and_ownership_policy_load(tmp_path):
    cfg = _load_yaml(tmp_path, """
company:
  name: c
  defaults:
    engine: {engine: echo}
teams:
  - name: luma-website
    roles: [{name: developer, kind: builder, prompt: build}]
    pipeline: {stages: [developer]}
    autonomy:
      actions:
        ready_pr: auto
        merge_pr: auto
        support_reply: auto
    ownership:
      enabled: true
      cycle_seconds: 300
      code:
        auto_ready: true
        auto_merge: true
        allowed_base_branches: [staging]
        required_checks: [test]
        deploy_workflow: Deploy to Staging
        live_url: https://luma-web-ai-staging.web.app
        smoke_paths: [/]
      support:
        auto_send_low_risk: true
        min_confidence: 0.92
""")
    team = cfg.team("luma-website")

    assert team.autonomy.actions["merge_pr"] == "auto"
    assert team.ownership.code.allowed_base_branches == ["staging"]
    assert team.ownership.support.min_confidence == 0.92


def test_vercel_ownership_policy_loads_exact_project_and_scope():
    ownership = _validate_ownership({
        "code": {
            "deploy_provider": "vercel",
            "deploy_project": "tadam-agents",
            "deploy_scope": "tadam-technology",
            "live_url": "https://tadam-agents-git-staging-tadam-technology.vercel.app",
        },
        "support": {"enabled": False},
    })

    assert ownership.code.deploy_provider == "vercel"
    assert ownership.code.deploy_project == "tadam-agents"
    assert ownership.code.deploy_scope == "tadam-technology"
    assert ownership.support.enabled is False


@pytest.mark.parametrize("ownership", [
    {"support": {"min_confidence": -0.01}},
    {"support": {"min_confidence": 1.01}},
    {"code": {"auto_merge": True}},
    {"code": {"auto_merge": True, "allowed_base_branches": ["main"]}},
    {"code": {"deploy_workflow": "Deploy"}},
    {"code": {"deploy_provider": "vercel"}},
    {"code": {
        "deploy_provider": "vercel",
        "deploy_project": "-unsafe-option",
        "deploy_scope": "team",
        "live_url": "https://example.test",
    }},
    {"code": {
        "deploy_provider": "vercel",
        "deploy_project": "project",
        "deploy_scope": "team",
        "deploy_workflow": "Deploy",
        "live_url": "https://example.test",
    }},
])
def test_invalid_ownership_policy_is_rejected(ownership):
    with pytest.raises(ValidationError):
        _validate_ownership(ownership)


@pytest.mark.parametrize("branch", ["main", "master", "production", "prod"])
def test_auto_merge_rejects_protected_production_branches(branch):
    with pytest.raises(ValidationError, match="protected production branch"):
        _validate_ownership({
            "code": {
                "auto_merge": True,
                "allowed_base_branches": [branch],
                "required_checks": ["test"],
            },
        })


@pytest.mark.parametrize(("ownership", "field"), [
    ({"cycle_seconds": 0}, "cycle_seconds"),
    ({"cycle_seconds": -1}, "cycle_seconds"),
    ({"max_active_obligations": 0}, "max_active_obligations"),
    ({"max_active_obligations": -1}, "max_active_obligations"),
    ({"max_attempts": 0}, "max_attempts"),
    ({"max_attempts": -1}, "max_attempts"),
    ({"stale_minutes": 0}, "stale_minutes"),
    ({"stale_minutes": -1}, "stale_minutes"),
    ({"maintenance": {"interval_hours": 0}}, "interval_hours"),
    ({"maintenance": {"interval_hours": -1}}, "interval_hours"),
    ({"maintenance": {"max_open": 0}}, "max_open"),
    ({"maintenance": {"max_open": -1}}, "max_open"),
    ({"code": {"deployment_timeout_minutes": 0}}, "deployment_timeout_minutes"),
    ({"code": {"deployment_timeout_minutes": -1}}, "deployment_timeout_minutes"),
])
def test_ownership_positive_values_reject_zero_and_negative(ownership, field):
    with pytest.raises(ValidationError, match=field):
        _validate_ownership(ownership)


@pytest.mark.parametrize("configured", [[], ["docs/private/**"]])
def test_code_blocked_globs_preserve_mandatory_baseline(configured):
    policy = _validate_ownership({"code": {"blocked_globs": configured}})

    assert set(schema.MANDATORY_OWNERSHIP_BLOCKED_GLOBS) <= set(
        policy.code.blocked_globs)
    assert set(configured) <= set(policy.code.blocked_globs)


@pytest.mark.parametrize("configured", [[], ["feature_request"]])
def test_support_blocked_categories_preserve_mandatory_baseline(configured):
    policy = _validate_ownership({"support": {"blocked_categories": configured}})

    assert set(schema.MANDATORY_OWNERSHIP_BLOCKED_CATEGORIES) <= set(
        policy.support.blocked_categories)
    assert set(configured) <= set(policy.support.blocked_categories)


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
        "    channels: [ { type: mastodon, role: control, channel_id: C123 } ]\n",
        encoding="utf-8",
    )

    with pytest.raises(loader.ConfigError, match="unsupported channel type 'mastodon'"):
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
