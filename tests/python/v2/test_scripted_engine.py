import os
from pathlib import Path

from argus.engine import EngineResult
from argus.engine.adapters import _proc
from argus.v2.worker import exec as job_exec
from argus.v2.queue.models import Job


def _job(snapshot, cwd):
    return Job(id="j1", request_id="r1", event_id=None, conversation_id=None,
               team_id="dev", role="developer", stage=0, kind="pipeline",
               status="running", attempts=0, max_attempts=3, claim_token="t",
               exec_snapshot=snapshot, payload={"text": "fix it"})


def test_scripted_engine_returns_canned_output(tmp_path):
    job = _job({"engine": "scripted", "prompt": "p",
                "scripted_output": "done\nARGUS_RESULT: {\"ready\": true}"}, str(tmp_path))
    run, result, actions = job_exec.run_job(None, job, workdir=str(tmp_path))
    assert "ARGUS_RESULT" in run.output and run.status == "ok"


def test_scripted_engine_writes_a_file_edit(tmp_path):
    job = _job({"engine": "scripted", "prompt": "p", "scripted_output": "ok",
                "scripted_edit": {"path": "fix.py", "content": "print(1)\n"}}, str(tmp_path))
    job_exec.run_job(None, job, workdir=str(tmp_path))
    assert (tmp_path / "fix.py").read_text() == "print(1)\n"


def test_run_job_restores_per_job_engine_env(monkeypatch, tmp_path):
    calls = []

    def fake_run_agent(engine, prompt):
        calls.append((os.environ.get("ARGUS_PROJECT"), os.environ.get("ARGUS_MODEL")))
        return EngineResult(text="ok")

    monkeypatch.delenv("ARGUS_PROJECT", raising=False)
    monkeypatch.delenv("ARGUS_MODEL", raising=False)
    monkeypatch.setattr(job_exec, "run_agent", fake_run_agent)

    job_exec.run_job(
        None,
        _job({"engine": "hermes", "prompt": "p", "project": "project-a", "model": "m1"},
             str(tmp_path)),
        workdir=str(tmp_path),
    )
    job_exec.run_job(
        None,
        _job({"engine": "hermes", "prompt": "p"}, str(tmp_path)),
        workdir=str(tmp_path),
    )

    assert calls == [("project-a", "m1"), (None, None)]
    assert "ARGUS_PROJECT" not in os.environ
    assert "ARGUS_MODEL" not in os.environ


def test_engine_runner_timeout_returns_none(tmp_path, monkeypatch):
    script = tmp_path / "sleep.py"
    script.write_text("import time\ntime.sleep(2)\n", encoding="utf-8")
    monkeypatch.setenv("ARGUS_ENGINE_TIMEOUT", "0.1")

    out = _proc.run_with_retries(["python3", str(script)], cwd=str(tmp_path))

    assert out is None
    assert "timed out" in _proc.last_stderr()
