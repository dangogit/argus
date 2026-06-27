"""Code mode: script extraction, and run_job executing a batched script inside
the worktree ONLY when the frozen allow_code_mode flag is set (flag off => the
marker is inert, no exec)."""
from types import SimpleNamespace

from argus.v2.worker import exec as je

_SCRIPT_OUT = 'ARGUS_SCRIPT:\n```sh\necho hi > out.txt\n```'


def test_extract_script_fenced():
    assert je.extract_script(_SCRIPT_OUT) == "echo hi > out.txt"


def test_extract_script_inline():
    assert je.extract_script("ARGUS_SCRIPT: touch x") == "touch x"


def test_extract_script_absent_or_unclosed():
    assert je.extract_script("no marker here") is None
    assert je.extract_script("ARGUS_SCRIPT:\n```sh\necho hi") is None  # never closed


def _job(snap):
    return SimpleNamespace(exec_snapshot=snap, payload={"text": "t"},
                           role="developer", id="job1")


def _patch_engine(monkeypatch, text):
    monkeypatch.setattr(je, "run_agent", lambda *a, **k: SimpleNamespace(
        text=text, cost_source="test", cost_usd=None))


def test_code_mode_runs_in_worktree_when_allowed(tmp_path, monkeypatch):
    _patch_engine(monkeypatch, _SCRIPT_OUT)
    job = _job({"engine": "fake", "allow_code_mode": True})
    run, out, actions = je.run_job(None, job, workdir=str(tmp_path))
    assert (tmp_path / "out.txt").read_text().strip() == "hi"   # script executed
    assert out["code_mode"]["exit"] == 0
    assert actions == []                                         # no ARGUS_ACTIONS present


def test_code_mode_inert_when_flag_off(tmp_path, monkeypatch):
    _patch_engine(monkeypatch, _SCRIPT_OUT)
    job = _job({"engine": "fake"})                              # no allow_code_mode
    run, out, actions = je.run_job(None, job, workdir=str(tmp_path))
    assert not (tmp_path / "out.txt").exists()                  # NOT executed
    assert "code_mode" not in out


def test_code_mode_captures_nonzero_exit(tmp_path, monkeypatch):
    _patch_engine(monkeypatch, 'ARGUS_SCRIPT:\n```sh\nexit 3\n```')
    job = _job({"engine": "fake", "allow_code_mode": True})
    _, out, _ = je.run_job(None, job, workdir=str(tmp_path))
    assert out["code_mode"]["exit"] == 3


def test_network_snapshot_sets_codex_env(tmp_path, monkeypatch):
    """snapshot network=True surfaces ARGUS_CODEX_NETWORK to the adapter during
    the run, and is cleaned up afterwards. Flag off => env stays unset."""
    import os
    seen = {}

    def _capture(*a, **k):
        seen["net"] = os.environ.get("ARGUS_CODEX_NETWORK")
        return SimpleNamespace(text="", cost_source="test", cost_usd=None)

    monkeypatch.setattr(je, "run_agent", _capture)
    monkeypatch.delenv("ARGUS_CODEX_NETWORK", raising=False)

    je.run_job(None, _job({"engine": "fake", "network": True}), workdir=str(tmp_path))
    assert seen["net"] == "1"
    assert "ARGUS_CODEX_NETWORK" not in os.environ  # restored

    je.run_job(None, _job({"engine": "fake"}), workdir=str(tmp_path))  # flag off
    assert seen["net"] is None
