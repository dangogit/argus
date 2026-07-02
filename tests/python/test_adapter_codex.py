import json
import os
import stat
import subprocess
import time
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


def test_timeout_failure_includes_recent_codex_checkpoints(tmp_path, monkeypatch, capsys):
    work = tmp_path / "repo"
    home = tmp_path / "codex-home"
    session = home / "sessions" / "2026" / "07" / "02" / "rollout-timeout.jsonl"
    work.mkdir()

    rows = [
        {"type": "session_meta", "payload": {"id": "s1", "cwd": str(work)}},
        {"type": "event_msg", "payload": {
            "type": "agent_message",
            "message": "investigated: found failed deploy",
        }},
        {"type": "event_msg", "payload": {
            "type": "agent_message",
            "message": "verified: health check passed TOKEN=abc123",
        }},
    ]
    session.parent.mkdir(parents=True)
    session.write_text("\n".join(json.dumps(row) for row in rows) + "\n",
                       encoding="utf-8")
    fresh = time.time() + 5
    os.utime(session, (fresh, fresh))
    _fake_codex(tmp_path, monkeypatch, "sleep 2\n")
    monkeypatch.setenv("ARGUS_AGENT_CWD", str(work))
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("ARGUS_ENGINE_TIMEOUT", "0.2")

    with pytest.raises(EngineOutageError) as exc:
        run_agent("codex", "repair production")

    text = str(exc.value)
    assert "process idle timed out after 0.2s" in text
    assert "Codex session:" in text
    assert "Recent checkpoints:" in text
    assert "investigated: found failed deploy" in text
    assert "verified: health check passed TOKEN=[REDACTED]" in text
    assert "abc123" not in text
    assert "Recent checkpoints:" in capsys.readouterr().err


def test_codex_transcript_heartbeat_changes_when_session_changes(tmp_path, monkeypatch):
    from argus.engine.adapters import codex

    work = tmp_path / "repo"
    home = tmp_path / "codex-home"
    session = home / "sessions" / "2026" / "07" / "02" / "rollout-active.jsonl"
    work.mkdir()
    session.parent.mkdir(parents=True)
    meta = {"type": "session_meta", "payload": {"id": "s1", "cwd": str(work)}}
    session.write_text(json.dumps(meta) + "\n", encoding="utf-8")
    investigated = {
        "type": "event_msg",
        "payload": {"type": "agent_message", "message": "investigated"},
    }
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("ARGUS_CODEX_TRANSCRIPT_POLL_INTERVAL", "0.05")

    heartbeat = codex._codex_progress_heartbeat(str(work), time.time() - 1)
    first = heartbeat()
    time.sleep(0.06)
    with session.open("a", encoding="utf-8") as f:
        f.write(json.dumps(investigated) + "\n")
    second = heartbeat()

    assert first is not None
    assert second is not None
    assert second != first


def test_timeout_progress_ignores_other_workdirs(tmp_path, monkeypatch):
    from argus.engine.adapters import codex

    wanted = tmp_path / "wanted"
    other = tmp_path / "other"
    home = tmp_path / "codex-home"
    wanted.mkdir()
    other.mkdir()

    def write_session(name, cwd, message):
        path = home / "sessions" / "2026" / "07" / "02" / f"rollout-{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"type": "session_meta", "payload": {"id": name, "cwd": str(cwd)}},
            {"type": "event_msg", "payload": {
                "type": "agent_message",
                "message": message,
            }},
        ]
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n",
                        encoding="utf-8")
        return path

    write_session("other", other, "wrong checkpoint")
    write_session("wanted", wanted, "right checkpoint")
    monkeypatch.setenv("CODEX_HOME", str(home))

    progress = codex._latest_progress_snapshot(str(wanted), time.time() - 1)

    assert progress is not None
    assert progress.checkpoints == ("right checkpoint",)


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
