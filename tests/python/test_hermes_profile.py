from pathlib import Path

from argus.hermes import profile


def _setup(tmp_path, monkeypatch, cfg_body=""):
    f = tmp_path / "argus.config.yaml"
    f.write_text(cfg_body)
    monkeypatch.setenv("ARGUS_CONFIG", str(f))
    monkeypatch.setenv("ARGUS_RUN_ROOT", str(tmp_path / "run"))
    monkeypatch.delenv("ARGUS_HERMES_HOME_ROOT", raising=False)


def test_ensure_profile_creates_home(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    home = profile.ensure_profile("luma")
    assert home == tmp_path / "run" / "hermes" / "luma"
    cfg = (home / "config.yaml").read_text()
    assert "max_turns: 30" in cfg
    assert "provider: \"anthropic\"" in cfg
    assert "agent-skills" in cfg  # default shared skills dir
    assert (home / "SOUL.md").is_file()
    soul = (home / "SOUL.md").read_text()
    assert "configured for propose-pr" in soul
    assert "Do not refuse a PM task" in soul


def test_ensure_profile_is_idempotent_and_preserves_edits(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    home = profile.ensure_profile("luma")
    (home / "config.yaml").write_text("model:\n  default: \"operator-edited\"\n")
    (home / "SOUL.md").write_text("# Custom SOUL\n")
    home2 = profile.ensure_profile("luma")
    assert home2 == home
    assert "operator-edited" in (home / "config.yaml").read_text()
    assert (home / "SOUL.md").read_text() == "# Custom SOUL\n"


def test_ensure_profile_updates_legacy_generated_soul(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    home = profile.ensure_profile("luma")
    legacy = """# SOUL

You are the dedicated Argus worker for the "luma" project. You run
unattended on a schedule. Rules that override everything else:

- Propose only. You never push, merge, deploy, or send messages; Argus owns
  every side effect and every approval.
- Be terse and factual. Your final message is consumed by a pipeline, not a
  human chat.
- Use your memory: record project-specific lessons, recall past sessions
  before repeating work.
"""
    (home / "SOUL.md").write_text(legacy)

    profile.ensure_profile("luma")

    soul = (home / "SOUL.md").read_text()
    assert "configured for propose-pr" in soul
    assert "Propose only. You never push" not in soul


def test_profile_honors_config_knobs(tmp_path, monkeypatch):
    _setup(
        tmp_path,
        monkeypatch,
        "hermes:\n  model: claude-opus-4-8\n  max_turns: 12\n  skills_dir: /shared/skills\n",
    )
    home = profile.ensure_profile("tadam")
    cfg = (home / "config.yaml").read_text()
    assert "claude-opus-4-8" in cfg
    assert "max_turns: 12" in cfg
    assert "/shared/skills" in cfg


def test_home_root_env_override(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("ARGUS_HERMES_HOME_ROOT", str(tmp_path / "custom"))
    home = profile.ensure_profile("luma")
    assert home == tmp_path / "custom" / "luma"


def test_skills_dir_is_quoted_and_sanitized(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, 'hermes:\n  skills_dir: "/path with spaces"\n')
    home = profile.ensure_profile("proj1")
    cfg = (home / "config.yaml").read_text()
    assert '- "/path with spaces"' in cfg


def test_project_name_is_sanitized(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    import pytest

    with pytest.raises(ValueError):
        profile.ensure_profile("../escape")
    with pytest.raises(ValueError):
        profile.ensure_profile("")
    with pytest.raises(ValueError):
        profile.ensure_profile("..")
    with pytest.raises(ValueError):
        profile.ensure_profile(".")


def test_provider_is_configurable(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "hermes:\n  provider: copilot\n  model: gpt-4.1\n")
    home = profile.ensure_profile("proj_prov")
    cfg = (home / "config.yaml").read_text()
    assert 'provider: "copilot"' in cfg
    assert 'default: "gpt-4.1"' in cfg


def test_provider_defaults_to_anthropic(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    home = profile.ensure_profile("proj_def")
    assert 'provider: "anthropic"' in (home / "config.yaml").read_text()
