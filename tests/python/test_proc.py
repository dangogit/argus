import os
import stat

from argus.engine.adapters._proc import last_stderr, run_with_retries


def _fake_bin(tmp_path, name, script):
    p = tmp_path / name
    p.write_text("#!/usr/bin/env bash\n" + script)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_success_returns_stdout(tmp_path):
    bin_ = _fake_bin(tmp_path, "ok", 'echo "agent says hi"\n')
    out = run_with_retries([bin_], cwd=str(tmp_path))
    assert out == "agent says hi\n"


def test_stdin_text_is_piped(tmp_path):
    bin_ = _fake_bin(tmp_path, "cat", "cat\n")
    out = run_with_retries([bin_], cwd=str(tmp_path), stdin_text="prompt body")
    assert out == "prompt body"


def test_hard_failure_returns_none(tmp_path):
    bin_ = _fake_bin(tmp_path, "boom", 'echo "bad request" >&2\nexit 1\n')
    assert run_with_retries([bin_], cwd=str(tmp_path)) is None
    assert last_stderr().strip() == "bad request"


def test_transient_failure_is_retried(tmp_path, monkeypatch):
    # Fails once with a rate-limit message, then succeeds via a marker file.
    marker = tmp_path / "tried"
    bin_ = _fake_bin(
        tmp_path,
        "flaky",
        f'if [ ! -f "{marker}" ]; then touch "{marker}"; echo "rate limit" >&2; exit 1; fi\necho recovered\n',
    )
    monkeypatch.setenv("ARGUS_ENGINE_RETRY_DELAY", "0")
    out = run_with_retries([bin_], cwd=str(tmp_path))
    assert out == "recovered\n"


def test_retry_budget_respected(tmp_path, monkeypatch):
    bin_ = _fake_bin(tmp_path, "always429", 'echo "429" >&2\nexit 1\n')
    monkeypatch.setenv("ARGUS_ENGINE_RETRY_DELAY", "0")
    monkeypatch.setenv("ARGUS_ENGINE_MAX_RETRIES", "1")
    assert run_with_retries([bin_], cwd=str(tmp_path)) is None


def test_missing_binary_returns_none(tmp_path):
    assert run_with_retries(["/nonexistent/binary"], cwd=str(tmp_path)) is None


def test_non_executable_returns_none(tmp_path):
    p = tmp_path / "notexec"
    p.write_text("#!/usr/bin/env bash\necho hi\n")
    assert run_with_retries([str(p)], cwd=str(tmp_path)) is None


def test_env_overlay_reaches_child(tmp_path):
    bin_ = _fake_bin(tmp_path, "envcheck", 'echo "HOME_IS:$HERMES_HOME"\n')
    out = run_with_retries([bin_], cwd=str(tmp_path), env={"HERMES_HOME": "/tmp/prof"})
    assert out == "HOME_IS:/tmp/prof\n"


def test_env_overlay_does_not_leak_into_parent(tmp_path):
    bin_ = _fake_bin(tmp_path, "noop", "exit 0\n")
    run_with_retries([bin_], cwd=str(tmp_path), env={"ARGUS_PROC_TEST_LEAK": "1"})
    assert os.environ.get("ARGUS_PROC_TEST_LEAK") is None
