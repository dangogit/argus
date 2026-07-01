"""Code mode env isolation: the model-authored script is untrusted (prompt
injection can steer it), so the subprocess must not inherit connector secrets
loaded into the worker process from ARGUS_ENV_FILES. Covers the minimal
allowlist, the non-login shell switch, and that normal exec/timeout behavior
is unchanged."""
import os
from types import SimpleNamespace

from argus.v2.worker import exec as je

_PRINT_ENV = 'ARGUS_SCRIPT:\n```sh\nenv\n```'


def _job(snap):
    return SimpleNamespace(exec_snapshot=snap, payload={"text": "t"},
                           role="developer", id="job1")


def _patch_engine(monkeypatch, text):
    monkeypatch.setattr(je, "run_agent", lambda *a, **k: SimpleNamespace(
        text=text, cost_source="test", cost_usd=None))


def test_code_mode_does_not_leak_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_supersecret")
    monkeypatch.setenv("ARGUS_TEST_SECRET", "do-not-leak")
    _patch_engine(monkeypatch, _PRINT_ENV)

    job = _job({"engine": "fake", "allow_code_mode": True})
    _, out, _ = je.run_job(None, job, workdir=str(tmp_path))

    output = out["code_mode"]["output"]
    assert "ghp_supersecret" not in output
    assert "do-not-leak" not in output
    assert "GITHUB_TOKEN" not in output
    assert "ARGUS_TEST_SECRET" not in output


def test_code_mode_env_has_path_and_home(tmp_path, monkeypatch):
    _patch_engine(monkeypatch, _PRINT_ENV)
    job = _job({"engine": "fake", "allow_code_mode": True})
    _, out, _ = je.run_job(None, job, workdir=str(tmp_path))

    output = out["code_mode"]["output"]
    assert f"PATH={os.environ['PATH']}" in output
    assert "HOME=" in output


def test_minimal_env_helper_allowlist(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_supersecret")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    env = je._minimal_env()
    assert set(env) <= {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}
    assert "GITHUB_TOKEN" not in env
    assert "SLACK_BOT_TOKEN" not in env
    assert env.get("LANG") == "en_US.UTF-8"


def test_code_mode_uses_non_login_shell(tmp_path, monkeypatch):
    """bash -lc would source ~/.zshenv or ~/.bash_profile, which can re-add
    secrets or PATH entries outside the allowlist. Verify the subprocess call
    uses -c, not -lc."""
    captured = {}
    real_run = je.subprocess.run

    def _spy(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(je.subprocess, "run", _spy)
    _patch_engine(monkeypatch, 'ARGUS_SCRIPT:\n```sh\ntrue\n```')
    job = _job({"engine": "fake", "allow_code_mode": True})
    je.run_job(None, job, workdir=str(tmp_path))

    assert captured["cmd"][:2] == ["bash", "-c"]
    assert captured["env"] is not None


def test_code_mode_normal_script_still_works(tmp_path, monkeypatch):
    script = 'ARGUS_SCRIPT:\n```sh\necho hi > out.txt\n```'
    _patch_engine(monkeypatch, script)
    job = _job({"engine": "fake", "allow_code_mode": True})
    _, out, _ = je.run_job(None, job, workdir=str(tmp_path))
    assert (tmp_path / "out.txt").read_text().strip() == "hi"
    assert out["code_mode"]["exit"] == 0


def test_code_mode_timeout_still_works(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_CODE_MODE_TIMEOUT", "0.2")
    script = 'ARGUS_SCRIPT:\n```sh\nsleep 5\n```'
    _patch_engine(monkeypatch, script)
    job = _job({"engine": "fake", "allow_code_mode": True})
    _, out, _ = je.run_job(None, job, workdir=str(tmp_path))
    assert out["code_mode"]["exit"] == 124
    assert "timed out" in out["code_mode"]["output"]
