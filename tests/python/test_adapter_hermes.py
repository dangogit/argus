# tests/python/test_adapter_hermes.py
import stat

import pytest

from argus.engine import EngineOutageError, run_agent


def _setup_env(tmp_path, monkeypatch):
    cfg = tmp_path / "argus.config.yaml"
    cfg.write_text("")
    monkeypatch.setenv("ARGUS_CONFIG", str(cfg))
    monkeypatch.setenv("ARGUS_RUN_ROOT", str(tmp_path / "run"))
    monkeypatch.setenv("ARGUS_PROJECT", "luma")
    monkeypatch.delenv("ARGUS_HERMES_HOME_ROOT", raising=False)


def _fake_hermes(tmp_path, monkeypatch, script):
    p = tmp_path / "hermes"
    p.write_text("#!/usr/bin/env bash\n" + script)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("ARGUS_HERMES_BIN", str(p))
    return p


def test_missing_binary_is_outage(tmp_path, monkeypatch, capsys):
    _setup_env(tmp_path, monkeypatch)
    monkeypatch.setenv("ARGUS_HERMES_BIN", "definitely-not-a-real-binary")
    with pytest.raises(EngineOutageError):
        run_agent("hermes", "hi")
    assert "hermes engine unavailable" in capsys.readouterr().err


def test_oneshot_invocation_shape(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    # The fake proves: -z flag, prompt as the next arg, -t toolsets, and
    # HERMES_HOME pointing at the project profile (created before the call).
    _fake_hermes(
        tmp_path,
        monkeypatch,
        'echo "ARGS:$*"\necho "HOME:$HERMES_HOME"\ntest -f "$HERMES_HOME/config.yaml" && echo "PROFILE:ok"\n',
    )
    result = run_agent("hermes", "triage the repo")
    assert "ARGS:-z triage the repo -t file,web,memory" in result.text
    assert "HOME:" in result.text and "/hermes/luma" in result.text
    assert "PROFILE:ok" in result.text
    assert result.cost_source == "estimated"


def test_default_profile_when_no_project(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    monkeypatch.delenv("ARGUS_PROJECT", raising=False)
    _fake_hermes(tmp_path, monkeypatch, 'echo "HOME:$HERMES_HOME"\n')
    result = run_agent("hermes", "x")
    assert "/hermes/_default" in result.text


def test_hard_failure_is_outage(tmp_path, monkeypatch, capsys):
    _setup_env(tmp_path, monkeypatch)
    _fake_hermes(tmp_path, monkeypatch, 'echo "agent blew up" >&2\nexit 1\n')
    with pytest.raises(EngineOutageError):
        run_agent("hermes", "x")
    assert "hermes engine failed" in capsys.readouterr().err


def test_toolset_env_override(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    _fake_hermes(tmp_path, monkeypatch, 'echo "ARGS:$*"\n')
    monkeypatch.setenv("ARGUS_HERMES_TOOLSETS", "coding")
    result = run_agent("hermes", "x")
    assert "-t coding" in result.text


def test_pm_qa_role_gets_terminal_toolset(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    _fake_hermes(tmp_path, monkeypatch, 'echo "ARGS:$*"\n')
    monkeypatch.setenv("ARGUS_PM_ROLE", "qa")
    result = run_agent("hermes", "x")
    assert "-t file,web,memory,terminal" in result.text


def test_pm_developer_role_gets_terminal_toolset(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    _fake_hermes(tmp_path, monkeypatch, 'echo "ARGS:$*"\n')
    monkeypatch.setenv("ARGUS_PM_ROLE", "developer")
    result = run_agent("hermes", "x")
    assert "-t file,web,memory,terminal" in result.text


def test_pm_researcher_role_is_read_only(tmp_path, monkeypatch):
    # The Researcher is grounded read-only; it must not get terminal by default,
    # matching the Claude path where only Developer/QA gained Bash.
    _setup_env(tmp_path, monkeypatch)
    _fake_hermes(tmp_path, monkeypatch, 'echo "ARGS:$*"\n')
    monkeypatch.setenv("ARGUS_PM_ROLE", "researcher")
    result = run_agent("hermes", "x")
    assert "-t file,web,memory\n" in result.text + "\n"
    assert "terminal" not in result.text


def test_pm_senior_review_role_is_read_only(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    _fake_hermes(tmp_path, monkeypatch, 'echo "ARGS:$*"\n')
    monkeypatch.setenv("ARGUS_PM_ROLE", "senior-review")
    result = run_agent("hermes", "x")
    assert "terminal" not in result.text


def test_pm_toolset_does_not_leak_terminal_to_read_only_roles(tmp_path, monkeypatch):
    # pm_toolset configures terminal-capable roles only; a read-only role keeps
    # its safe default even when pm_toolset includes terminal.
    cfg = tmp_path / "argus.config.yaml"
    cfg.write_text("hermes:\n  pm_toolset: file,web,memory,terminal\n")
    monkeypatch.setenv("ARGUS_CONFIG", str(cfg))
    monkeypatch.setenv("ARGUS_RUN_ROOT", str(tmp_path / "run"))
    monkeypatch.setenv("ARGUS_PROJECT", "luma")
    monkeypatch.setenv("ARGUS_PM_ROLE", "researcher")
    monkeypatch.delenv("ARGUS_HERMES_HOME_ROOT", raising=False)
    _fake_hermes(tmp_path, monkeypatch, 'echo "ARGS:$*"\n')
    result = run_agent("hermes", "x")
    assert "terminal" not in result.text


def test_pm_read_only_role_toolset_can_be_configured(tmp_path, monkeypatch):
    cfg = tmp_path / "argus.config.yaml"
    cfg.write_text("hermes:\n  pm_researcher_toolset: file,web,memory,terminal\n")
    monkeypatch.setenv("ARGUS_CONFIG", str(cfg))
    monkeypatch.setenv("ARGUS_RUN_ROOT", str(tmp_path / "run"))
    monkeypatch.setenv("ARGUS_PROJECT", "luma")
    monkeypatch.setenv("ARGUS_PM_ROLE", "researcher")
    monkeypatch.delenv("ARGUS_HERMES_HOME_ROOT", raising=False)
    _fake_hermes(tmp_path, monkeypatch, 'echo "ARGS:$*"\n')
    result = run_agent("hermes", "x")
    assert "-t file,web,memory,terminal" in result.text


def test_pm_role_toolset_can_be_configured(tmp_path, monkeypatch):
    cfg = tmp_path / "argus.config.yaml"
    cfg.write_text("hermes:\n  pm_toolset: terminal,file\n")
    monkeypatch.setenv("ARGUS_CONFIG", str(cfg))
    monkeypatch.setenv("ARGUS_RUN_ROOT", str(tmp_path / "run"))
    monkeypatch.setenv("ARGUS_PROJECT", "luma")
    monkeypatch.setenv("ARGUS_PM_ROLE", "developer")
    monkeypatch.delenv("ARGUS_HERMES_HOME_ROOT", raising=False)
    _fake_hermes(tmp_path, monkeypatch, 'echo "ARGS:$*"\n')
    result = run_agent("hermes", "x")
    assert "-t terminal,file" in result.text


def test_cost_read_from_state_db(tmp_path, monkeypatch):
    import sqlite3

    _setup_env(tmp_path, monkeypatch)
    _fake_hermes(tmp_path, monkeypatch, 'echo done\n')
    home = tmp_path / "run" / "hermes" / "luma"
    home.mkdir(parents=True)
    db = sqlite3.connect(home / "state.db")
    db.execute("CREATE TABLE sessions (id TEXT, source TEXT, started_at TEXT, estimated_cost_usd REAL)")
    db.execute("INSERT INTO sessions VALUES ('s1','cli','2026-06-12T01:00:00',0.07)")
    db.commit()
    db.close()
    result = run_agent("hermes", "x")
    assert result.cost_usd == "0.07"
