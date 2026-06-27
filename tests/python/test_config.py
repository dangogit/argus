from pathlib import Path

import pytest

from argus import config


def test_config_path_prefers_explicit_env(monkeypatch, tmp_path):
    explicit = tmp_path / "custom.yaml"
    monkeypatch.setenv("ARGUS_CONFIG", str(explicit))
    # bash parity: ARGUS_CONFIG wins even if the file does not exist;
    # existence is config_get's problem.
    assert config.config_path() == explicit


def test_config_path_falls_back_to_repo_root(monkeypatch):
    monkeypatch.delenv("ARGUS_CONFIG", raising=False)
    p = config.config_path()
    # The dev repo has argus.config.yaml at its root only if the user created
    # one; both outcomes are valid.
    assert p is None or p.name == "argus.config.yaml"


SAMPLE = """\
# top comment
run_root: /tmp/run  # inline comment
quoted: "hello world"
single: 'one two'
empty:
nul: null
tilde: ~
engine:
  default: claude-code
  fallback: codex   # comment
notifier:
  default: console
"""


def _cfg(tmp_path, monkeypatch, text=SAMPLE):
    f = tmp_path / "argus.config.yaml"
    f.write_text(text)
    monkeypatch.setenv("ARGUS_CONFIG", str(f))
    return f


def test_bare_top_level_key(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    assert config.config_get("run_root") == "/tmp/run"


def test_nested_block_key(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    assert config.config_get("engine.default") == "claude-code"
    assert config.config_get("engine.fallback") == "codex"
    assert config.config_get("notifier.default") == "console"


def test_quotes_are_stripped(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    assert config.config_get("quoted") == "hello world"
    assert config.config_get("single") == "one two"


def test_null_tilde_empty_are_unset(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    assert config.config_get("empty") is None
    assert config.config_get("nul") is None
    assert config.config_get("tilde") is None


def test_missing_key_and_missing_file(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    assert config.config_get("nope") is None
    assert config.config_get("engine.nope") is None
    monkeypatch.setenv("ARGUS_CONFIG", str(tmp_path / "absent.yaml"))
    assert config.config_get("run_root") is None


# Extra coverage matching bats test 2 (overlay.dir path value).
def test_nested_path_value(tmp_path, monkeypatch):
    _cfg(
        tmp_path,
        monkeypatch,
        "overlay:\n  dir: /tmp/some-overlay\n",
    )
    assert config.config_get("overlay.dir") == "/tmp/some-overlay"


# Empty key raises ValueError (parity with bash "key required").
def test_empty_key_raises(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        config.config_get("")


def test_hash_in_value_truncates_like_bash(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch, "token: abc#def\n")
    assert config.config_get("token") == "abc"


def test_run_root_env_wins(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch, "run_root: /cfg/run\n")
    monkeypatch.setenv("ARGUS_RUN_ROOT", "/env/run")
    assert config.run_root() == Path("/env/run")


def test_run_root_config_then_default(tmp_path, monkeypatch):
    monkeypatch.delenv("ARGUS_RUN_ROOT", raising=False)
    _cfg(tmp_path, monkeypatch, "run_root: /cfg/run\n")
    assert config.run_root() == Path("/cfg/run")
    monkeypatch.setenv("ARGUS_CONFIG", str(tmp_path / "absent.yaml"))
    assert config.run_root() == config.repo_root() / "run"
