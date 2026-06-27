import pytest

from argus.cli import main


def test_engine_list(capsys):
    rc = main(["engine", "list"])
    assert rc == 0
    assert capsys.readouterr().out.splitlines() == ["claude-code", "codex", "echo", "hermes"]


def test_engine_run_echo(capsys):
    rc = main(["engine", "run", "--engine", "echo", "--prompt", "hello"])
    assert rc == 0
    assert capsys.readouterr().out == "ECHO: hello\n"


def test_engine_run_show_cost(capsys):
    rc = main(["engine", "run", "--engine", "echo", "--show-cost", "--prompt", "hi"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("ECHO: hi\n")
    assert "cost: source=unpriced usd=\n" in out


def test_unknown_engine_exits_3(capsys):
    rc = main(["engine", "run", "--engine", "nope", "--prompt", "x"])
    assert rc == 3
    assert "unknown engine: nope" in capsys.readouterr().err


def test_outage_exits_42(monkeypatch, capsys):
    monkeypatch.setenv("ARGUS_CLAUDE_BIN", "definitely-not-a-real-binary")
    rc = main(["engine", "run", "--engine", "claude-code", "--prompt", "x"])
    assert rc == 42


def test_unknown_option_dies_like_bash(capsys):
    rc = main(["engine", "run", "--bogus", "x"])
    assert rc == 1
    assert "[argus] error: unknown engine run option: --bogus" in capsys.readouterr().err


def test_flag_missing_value_dies(capsys):
    rc = main(["engine", "run", "--engine"])
    assert rc == 1
    assert "needs a value" in capsys.readouterr().err


def test_outage_with_show_cost_prints_cost_line(monkeypatch, capsys):
    monkeypatch.setenv("ARGUS_CLAUDE_BIN", "definitely-not-a-real-binary")
    rc = main(["engine", "run", "--engine", "claude-code", "--show-cost", "--prompt", "x"])
    assert rc == 42
    assert "cost: source=unpriced usd=\n" in capsys.readouterr().out


def test_show_cost_does_not_leak_env(monkeypatch, capsys):
    """ARGUS_ENGINE_META must not persist after the call."""
    import os
    monkeypatch.delenv("ARGUS_ENGINE_META", raising=False)
    main(["engine", "run", "--engine", "echo", "--show-cost", "--prompt", "leak"])
    assert "ARGUS_ENGINE_META" not in os.environ
