import stat

import pytest

from argus.engine import EngineOutageError, run_agent


def _fake_claude(tmp_path, monkeypatch, script):
    p = tmp_path / "claude"
    p.write_text("#!/usr/bin/env bash\n" + script)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("ARGUS_CLAUDE_BIN", str(p))
    return p


def test_missing_binary_is_outage(monkeypatch, capsys):
    monkeypatch.setenv("ARGUS_CLAUDE_BIN", "definitely-not-a-real-binary")
    with pytest.raises(EngineOutageError):
        run_agent("claude-code", "hi")
    assert "claude-code engine unavailable" in capsys.readouterr().err


def test_prompt_via_stdin_and_default_args(tmp_path, monkeypatch):
    # The fake echoes its argv then its stdin, so we can assert both.
    _fake_claude(tmp_path, monkeypatch, 'echo "ARGS:$*"\ncat\n')
    monkeypatch.delenv("ARGUS_CLAUDE_TOOLS", raising=False)
    monkeypatch.delenv("ARGUS_ENGINE_IGNORE_USER_CONFIG", raising=False)
    result = run_agent("claude-code", "the prompt")
    assert "--print" in result.text
    assert "--permission-mode default" in result.text
    assert "--tools Read,Grep,Glob" in result.text
    assert "--strict-mcp-config" not in result.text
    assert result.text.endswith("the prompt")
    assert result.cost_source == "unpriced"


def test_empty_tools_env_is_honored(tmp_path, monkeypatch):
    # bash uses ${ARGUS_CLAUDE_TOOLS-default}: set-but-empty must stay empty.
    _fake_claude(tmp_path, monkeypatch, 'echo "ARGS:$*"\n')
    monkeypatch.setenv("ARGUS_CLAUDE_TOOLS", "")
    result = run_agent("claude-code", "x")
    assert "--tools Read,Grep,Glob" not in result.text


def test_hermetic_flag(tmp_path, monkeypatch):
    _fake_claude(tmp_path, monkeypatch, 'echo "ARGS:$*"\n')
    monkeypatch.setenv("ARGUS_ENGINE_IGNORE_USER_CONFIG", "1")
    result = run_agent("claude-code", "x")
    assert "--strict-mcp-config" in result.text


def test_hard_failure_is_outage(tmp_path, monkeypatch, capsys):
    _fake_claude(tmp_path, monkeypatch, 'echo "fatal" >&2\nexit 1\n')
    with pytest.raises(EngineOutageError):
        run_agent("claude-code", "x")
    assert "exited non-zero" in capsys.readouterr().err
