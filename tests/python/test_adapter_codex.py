import stat
import subprocess
from pathlib import Path

import pytest

from argus.engine import EngineOutageError, run_agent


def _fake_codex(tmp_path, monkeypatch, script='echo "ARGS:$*"\ncat 2>/dev/null || true\n'):
    p = tmp_path / "codex"
    p.write_text("#!/usr/bin/env bash\n" + script)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("ARGUS_CODEX_BIN", str(p))
    return p


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _resolved_git_dir(cwd, flag):
    raw = _git(cwd, "rev-parse", flag).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = Path(cwd) / path
    return str(path.resolve())


def test_missing_binary_is_outage(monkeypatch, capsys):
    monkeypatch.setenv("ARGUS_CODEX_BIN", "definitely-not-a-real-binary")
    with pytest.raises(EngineOutageError):
        run_agent("codex", "hi")
    assert "codex engine unavailable" in capsys.readouterr().err


def test_hard_failure_includes_stderr_detail(tmp_path, monkeypatch, capsys):
    _fake_codex(tmp_path, monkeypatch, 'echo "bad auth" >&2\nexit 1\n')
    with pytest.raises(EngineOutageError) as exc:
        run_agent("codex", "hi")
    assert "bad auth" in str(exc.value)
    assert "bad auth" in capsys.readouterr().err


def test_argv_prompt_and_sandbox_default(tmp_path, monkeypatch):
    _fake_codex(tmp_path, monkeypatch)
    monkeypatch.delenv("ARGUS_CODEX_STDIN", raising=False)
    monkeypatch.delenv("ARGUS_ENGINE_IGNORE_USER_CONFIG", raising=False)
    result = run_agent("codex", "fix the bug")
    assert "exec" in result.text
    assert "--sandbox workspace-write" in result.text
    assert "fix the bug" in result.text
    assert "--ignore-user-config" not in result.text
    assert result.cost_source == "estimated"


def test_stdin_mode_passes_dash(tmp_path, monkeypatch):
    _fake_codex(tmp_path, monkeypatch)
    monkeypatch.setenv("ARGUS_CODEX_STDIN", "1")
    result = run_agent("codex", "secret prompt")
    assert result.text.splitlines()[0].endswith("-")
    assert "secret prompt" not in result.text.splitlines()[0]  # not in argv
    assert "secret prompt" in result.text  # arrived via stdin


def test_hermetic_flag(tmp_path, monkeypatch):
    _fake_codex(tmp_path, monkeypatch)
    monkeypatch.setenv("ARGUS_ENGINE_IGNORE_USER_CONFIG", "1")
    result = run_agent("codex", "x")
    assert "--ignore-user-config" in result.text


def test_network_flag_opens_workspace_write_network(tmp_path, monkeypatch):
    """ARGUS_CODEX_NETWORK=1 adds the network_access config override (so gh/git
    can reach GitHub) while keeping the write-confined workspace-write sandbox."""
    _fake_codex(tmp_path, monkeypatch)
    monkeypatch.setenv("ARGUS_CODEX_NETWORK", "1")
    result = run_agent("codex", "push the branch")
    assert "--sandbox workspace-write" in result.text
    assert "sandbox_workspace_write.network_access=true" in result.text


def test_network_flag_off_by_default(tmp_path, monkeypatch):
    _fake_codex(tmp_path, monkeypatch)
    monkeypatch.delenv("ARGUS_CODEX_NETWORK", raising=False)
    result = run_agent("codex", "x")
    assert "network_access" not in result.text


def test_network_flag_ignored_outside_workspace_write(tmp_path, monkeypatch):
    """Network override only applies to workspace-write; never widens read-only."""
    _fake_codex(tmp_path, monkeypatch)
    monkeypatch.setenv("ARGUS_CODEX_NETWORK", "1")
    monkeypatch.setenv("ARGUS_CODEX_SANDBOX", "read-only")
    result = run_agent("codex", "x")
    assert "network_access" not in result.text


def test_workspace_write_adds_linked_worktree_git_metadata(tmp_path, monkeypatch):
    """Linked worktrees need both per-worktree and common Git metadata writable."""
    _fake_codex(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    _git(repo, "worktree", "add", "-b", "feature", str(wt))

    monkeypatch.setenv("ARGUS_AGENT_CWD", str(wt))
    result = run_agent("codex", "change it")

    assert f"--add-dir {_resolved_git_dir(wt, '--git-dir')}" in result.text
    assert f"--add-dir {_resolved_git_dir(wt, '--git-common-dir')}" in result.text
