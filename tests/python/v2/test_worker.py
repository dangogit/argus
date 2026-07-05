import subprocess
import time
from threading import Event

from argus.v2.queue import jobs
from argus.v2.ingress import events
from argus.v2.config import loader
from argus.v2.pm import memory as pm_memory
from argus.v2.queue.models import RunRecord
from argus.v2.worker import worker
from argus.v2.workspace.repo import Worktree


def test_worker_runs_echo_job_to_done(conn, cfg, monkeypatch):
    jobs.enqueue(conn, team_id="dev", kind="pipeline", role="developer", stage=0,
                 idempotency_key="k1", exec_snapshot={"engine": "echo", "prompt": "do it"},
                 payload={"text": "fix login"})
    conn.commit()
    did = worker.run_once(cfg, "w1")
    assert did is True
    with conn.cursor() as cur:
        cur.execute("SELECT status, result FROM jobs WHERE idempotency_key='k1'")
        status, result = cur.fetchone()
    assert status == "done"
    assert "ECHO:" in result["output"]
    with conn.cursor() as cur:
        cur.execute("SELECT engine, status FROM runs")
        assert cur.fetchone() == ("echo", "ok")


def test_worker_run_carries_prompt_hash_from_snapshot(conn, cfg):
    """The prompt_hash frozen into exec_snapshot at enqueue must end up on the
    runs row so a regression can be correlated back to the prompt version."""
    jobs.enqueue(conn, team_id="dev", kind="pipeline", role="developer", stage=0,
                 idempotency_key="k-hash",
                 exec_snapshot={"engine": "echo", "prompt": "do it", "prompt_hash": "abc123def456"},
                 payload={"text": "fix login"})
    conn.commit()
    assert worker.run_once(cfg, "w1") is True
    with conn.cursor() as cur:
        cur.execute("SELECT prompt_hash FROM runs WHERE job_id="
                    "(SELECT id FROM jobs WHERE idempotency_key='k-hash')")
        assert cur.fetchone() == ("abc123def456",)


def test_worker_returns_false_when_idle(conn, cfg):
    assert worker.run_once(cfg, "w1") is False


def test_worker_chat_lane_claims_only_chat_kinds(conn, cfg):
    """The chat lane (include_kinds) runs the converse job and leaves the pipeline
    job pending for the other lane - so a build can never block a chat reply."""
    jobs.enqueue(conn, team_id="dev", kind="pipeline", role="developer", stage=0,
                 idempotency_key="pj", exec_snapshot={"engine": "echo"}, payload={"text": "x"})
    jobs.enqueue(conn, team_id="dev", kind="converse", role="manager", stage=0,
                 idempotency_key="cj", exec_snapshot={"engine": "echo"}, payload={"text": "hi"})
    conn.commit()
    ran = worker.run_once(cfg, "chat", include_kinds=list(jobs.CHAT_KINDS))
    assert ran is True  # claimed + ran the converse job
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM jobs WHERE idempotency_key='pj'")
        assert cur.fetchone()[0] == "pending"   # pipeline untouched by the chat lane
        cur.execute("SELECT status FROM jobs WHERE idempotency_key='cj'")
        assert cur.fetchone()[0] != "pending"   # converse was picked up


def test_worker_pipeline_lane_skips_chat(conn, cfg):
    """The pipeline lane (exclude_kinds) ignores a converse job."""
    jobs.enqueue(conn, team_id="dev", kind="converse", role="manager", stage=0,
                 idempotency_key="cj", exec_snapshot={"engine": "echo"}, payload={"text": "hi"})
    conn.commit()
    assert worker.run_once(cfg, "pipe", exclude_kinds=list(jobs.CHAT_KINDS)) is False
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM jobs WHERE idempotency_key='cj'")
        assert cur.fetchone()[0] == "pending"


def test_worker_finalizes_exception_as_failed(conn, cfg_project, monkeypatch):
    eid = events.ingest_message(conn, cfg_project, team="dev", source="cli",
                                dedup_key="worker-exception", text="check it")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO requests (event_id, team_id) VALUES (%s,'dev') RETURNING id",
                    (eid,))
        rid = str(cur.fetchone()[0])
    jobs.enqueue(conn, team_id="dev", kind="pipeline", role="developer", stage=0,
                 idempotency_key="worker-exception", exec_snapshot={"engine": "echo"},
                 payload={"text": "check it"}, request_id=rid, event_id=eid)
    conn.commit()

    def fail_create_worktree(project, request_id):
        raise RuntimeError("worktree boom")

    monkeypatch.setattr(worker.workspace, "create_worktree", fail_create_worktree)

    assert worker.run_once(cfg_project, "w1") is True
    with conn.cursor() as cur:
        cur.execute("SELECT status, result FROM jobs WHERE idempotency_key='worker-exception'")
        status, result = cur.fetchone()
        assert status == "failed"
        assert result["error_type"] == "RuntimeError"
        assert "worktree boom" in result["error"]
        cur.execute("SELECT status, output FROM runs")
        run_status, output = cur.fetchone()
        assert run_status == "failed"
        assert "Worker failed before completion" in output


def test_worker_passes_test_output_to_qa(conn, tmp_path, monkeypatch):
    cfg_path = tmp_path / "argus.yaml"
    cfg_path.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n  - name: dev\n"
        f"    project: {{ repo: {tmp_path}, base_branch: main, test_cmd: \"printf boom; exit 7\" }}\n"
        "    roles: [ { name: qa, kind: judge, prompt: p } ]\n"
        "    pipeline: { stages: [qa] }\n",
        encoding="utf-8",
    )
    cfg = loader.load(cfg_path)
    eid = events.ingest_message(conn, cfg, team="dev", source="cli",
                                dedup_key="qa-test-output", text="check it")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO requests (event_id, team_id) VALUES (%s,'dev') RETURNING id",
                    (eid,))
        rid = str(cur.fetchone()[0])
    jobs.enqueue(conn, team_id="dev", kind="pipeline", role="qa", stage=0,
                 idempotency_key="qa-test-output", exec_snapshot={"engine": "echo"},
                 payload={"text": "check it"}, request_id=rid, event_id=eid)
    conn.commit()

    monkeypatch.setattr(worker.workspace, "create_worktree",
                        lambda project, request_id: Worktree(str(tmp_path), "b", str(tmp_path)))
    seen = {}

    def fake_run_job(cfg, job, context, workdir):
        seen["context"] = context
        return (
            RunRecord(role=job.role, engine="echo", status="ok",
                      output='ARGUS_RESULT: {"verdict": "pass"}'),
            {},
            [],
        )

    monkeypatch.setattr(worker.job_exec, "run_job", fake_run_job)

    assert worker.run_once(cfg, "w1") is True
    assert "TEST RESULT" in seen["context"]
    assert "exit_code: 7" in seen["context"]
    assert "boom" in seen["context"]
    with conn.cursor() as cur:
        cur.execute("SELECT result FROM jobs WHERE idempotency_key='qa-test-output'")
        result = cur.fetchone()[0]
    assert result["test_exit"] == 7
    assert "boom" in result["test_output"]


def test_worker_marks_postgres_test_failure_as_qa_blocker(conn, tmp_path, monkeypatch):
    cfg_path = tmp_path / "argus.yaml"
    cfg_path.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n  - name: dev\n"
        f"    project: {{ repo: {tmp_path}, base_branch: main, test_cmd: \"psql postgresql://localhost/argus -c select\" }}\n"
        "    roles: [ { name: qa, kind: judge, prompt: p } ]\n"
        "    pipeline: { stages: [qa] }\n",
        encoding="utf-8",
    )
    cfg = loader.load(cfg_path)
    eid = events.ingest_message(conn, cfg, team="dev", source="cli",
                                dedup_key="qa-postgres-blocker", text="check it")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO requests (event_id, team_id) VALUES (%s,'dev') RETURNING id",
                    (eid,))
        rid = str(cur.fetchone()[0])
    jobs.enqueue(conn, team_id="dev", kind="pipeline", role="qa", stage=0,
                 idempotency_key="qa-postgres-blocker", exec_snapshot={"engine": "echo"},
                 payload={"text": "check it"}, request_id=rid, event_id=eid)
    conn.commit()

    monkeypatch.setattr(worker.workspace, "create_worktree",
                        lambda project, request_id: Worktree(str(tmp_path), "b", str(tmp_path)))

    test_run = subprocess.CompletedProcess(
        args="psql",
        returncode=2,
        stdout="",
        stderr="psql: error: connection to server at \"localhost\" (::1), port 5432 failed: Connection refused",
    )
    monkeypatch.setattr(worker.subprocess, "run", lambda *args, **kwargs: test_run)

    def fake_run_job(cfg, job, context, workdir):
        return (
            RunRecord(role=job.role, engine="echo", status="ok",
                      output='ARGUS_RESULT: {"verdict": "pass"}'),
            {},
            [],
        )

    monkeypatch.setattr(worker.job_exec, "run_job", fake_run_job)

    assert worker.run_once(cfg, "w1") is True
    with conn.cursor() as cur:
        cur.execute("SELECT result FROM jobs WHERE idempotency_key='qa-postgres-blocker'")
        result = cur.fetchone()[0]
    assert result["test_exit"] == 2
    assert result["qa_environment_blocker"] == "postgres-unavailable"
    assert result["parsed"]["verdict"] == "fail"
    assert result["parsed"]["qa_environment_blocker"] == "postgres-unavailable"


def test_worker_turns_qa_test_timeout_into_context(conn, tmp_path, monkeypatch):
    cfg_path = tmp_path / "argus.yaml"
    cfg_path.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n  - name: dev\n"
        f"    project: {{ repo: {tmp_path}, base_branch: main, test_cmd: \"sleep 99\", test_timeout_seconds: 3 }}\n"
        "    roles: [ { name: qa, kind: judge, prompt: p } ]\n"
        "    pipeline: { stages: [qa] }\n",
        encoding="utf-8",
    )
    cfg = loader.load(cfg_path)
    eid = events.ingest_message(conn, cfg, team="dev", source="cli",
                                dedup_key="qa-test-timeout", text="check it")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO requests (event_id, team_id) VALUES (%s,'dev') RETURNING id",
                    (eid,))
        rid = str(cur.fetchone()[0])
    jobs.enqueue(conn, team_id="dev", kind="pipeline", role="qa", stage=0,
                 idempotency_key="qa-test-timeout", exec_snapshot={"engine": "echo"},
                 payload={"text": "check it"}, request_id=rid, event_id=eid)
    conn.commit()

    monkeypatch.setattr(worker.workspace, "create_worktree",
                        lambda project, request_id: Worktree(str(tmp_path), "b", str(tmp_path)))

    def timeout_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=kwargs.get("args", "sleep 99"), timeout=3)

    monkeypatch.setattr(worker.subprocess, "run", timeout_run)
    seen = {}

    def fake_run_job(cfg, job, context, workdir):
        seen["context"] = context
        return (
            RunRecord(role=job.role, engine="echo", status="ok",
                      output='ARGUS_RESULT: {"verdict": "fail"}'),
            {},
            [],
        )

    monkeypatch.setattr(worker.job_exec, "run_job", fake_run_job)

    assert worker.run_once(cfg, "w1") is True
    assert "TEST RESULT" in seen["context"]
    assert "exit_code: 124" in seen["context"]
    assert "timed out after 3s" in seen["context"]
    with conn.cursor() as cur:
        cur.execute("SELECT result FROM jobs WHERE idempotency_key='qa-test-timeout'")
        result = cur.fetchone()[0]
    assert result["test_exit"] == 124
    assert "timed out after 3s" in result["test_output"]


def test_builder_without_diff_marks_not_ready(conn, tmp_path, monkeypatch):
    cfg_path = tmp_path / "argus.yaml"
    cfg_path.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n  - name: dev\n"
        f"    project: {{ repo: {tmp_path}, base_branch: main }}\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n",
        encoding="utf-8",
    )
    cfg = loader.load(cfg_path)
    eid = events.ingest_message(conn, cfg, team="dev", source="cli",
                                dedup_key="builder-no-diff", text="check it")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO requests (event_id, team_id) VALUES (%s,'dev') RETURNING id",
                    (eid,))
        rid = str(cur.fetchone()[0])
    jobs.enqueue(conn, team_id="dev", kind="pipeline", role="developer", stage=0,
                 idempotency_key="builder-no-diff", exec_snapshot={"engine": "echo"},
                 payload={"text": "check it"}, request_id=rid, event_id=eid)
    conn.commit()

    monkeypatch.setattr(worker.workspace, "create_worktree",
                        lambda project, request_id: Worktree(str(tmp_path), "b", str(tmp_path)))
    monkeypatch.setattr(worker.workspace, "commit_all", lambda path, message: False)
    monkeypatch.setattr(worker.workspace, "diff", lambda project, path: "")

    assert worker.run_once(cfg, "w1") is True
    with conn.cursor() as cur:
        cur.execute("SELECT result FROM jobs WHERE idempotency_key='builder-no-diff'")
        result = cur.fetchone()[0]
    assert result["has_diff"] is False
    assert result["parsed"]["ready"] is False


def test_worker_injects_project_memory_into_builder(conn, tmp_path, monkeypatch):
    cfg_path = tmp_path / "argus.yaml"
    cfg_path.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n  - name: dev\n"
        f"    project: {{ repo: {tmp_path}, base_branch: main }}\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n",
        encoding="utf-8",
    )
    cfg = loader.load(cfg_path)
    pm_memory.append(conn, team_id="dev", fingerprint="prior",
                     finding="previous failed approach", outcome="qa-fail")
    eid = events.ingest_message(conn, cfg, team="dev", source="cli",
                                dedup_key="builder-memory", text="check it")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO requests (event_id, team_id) VALUES (%s,'dev') RETURNING id",
                    (eid,))
        rid = str(cur.fetchone()[0])
    jobs.enqueue(conn, team_id="dev", kind="pipeline", role="developer", stage=0,
                 idempotency_key="builder-memory", exec_snapshot={"engine": "echo"},
                 payload={"text": "check it"}, request_id=rid, event_id=eid)
    conn.commit()

    monkeypatch.setattr(worker.workspace, "create_worktree",
                        lambda project, request_id: Worktree(str(tmp_path), "b", str(tmp_path)))
    monkeypatch.setattr(worker.workspace, "commit_all", lambda path, message: True)
    monkeypatch.setattr(worker.workspace, "diff", lambda project, path: "diff")
    seen = {}

    def fake_run_job(cfg, job, context, workdir):
        seen["context"] = context
        return (
            RunRecord(role=job.role, engine="echo", status="ok",
                      output='ARGUS_RESULT: {"ready": true}'),
            {},
            [],
        )

    monkeypatch.setattr(worker.job_exec, "run_job", fake_run_job)

    assert worker.run_once(cfg, "w1") is True
    assert "Project memory" in seen["context"]
    assert "previous failed approach" in seen["context"]
    with conn.cursor() as cur:
        cur.execute("SELECT result FROM jobs WHERE idempotency_key='builder-memory'")
        result = cur.fetchone()[0]
    assert result["memory_fingerprints"] == ["prior"]


def test_heartbeat_reconnects_after_poisoned_connection(conn, cfg, pg_dsn, monkeypatch):
    """A heartbeat connection poisoned mid-run (server restart, network blip)
    must not stop lease renewal silently: the beat loop should close it,
    reconnect, and keep renewing so the job is never reclaimed out from under
    a still-running worker."""
    jobs.enqueue(conn, team_id="dev", kind="pipeline", role="developer", stage=0,
                 idempotency_key="hb1", exec_snapshot={"engine": "echo"},
                 payload={"text": "x"})
    conn.commit()
    job = jobs.claim(conn, "w1")
    conn.commit()
    assert job is not None

    real_connect = worker.pool.connect
    calls = {"n": 0}

    def flaky_connect():
        calls["n"] += 1
        c = real_connect()
        if calls["n"] == 1:
            # Poison this connection: it looks alive but every heartbeat on it
            # will raise, simulating a server restart/network blip.
            c.close()
        return c

    monkeypatch.setattr(worker.pool, "connect", flaky_connect)
    monkeypatch.setattr(worker, "_HEARTBEAT_INTERVAL", 0.05, raising=False)

    stop = Event()
    thread = worker._start_heartbeat(job.id, job.claim_token, stop)
    try:
        deadline = time.time() + 5
        renewed = False
        while time.time() < deadline:
            with real_connect() as c, c.cursor() as cur:
                cur.execute("SELECT heartbeat_at, status FROM jobs WHERE id=%s", (job.id,))
                heartbeat_at, status = cur.fetchone()
            if heartbeat_at is not None and status == "running":
                renewed = True
                break
            time.sleep(0.1)
        assert renewed, "heartbeat never renewed after the first connection was poisoned"
        assert calls["n"] >= 2, "heartbeat did not reconnect after the failure"
    finally:
        worker._stop_heartbeat(stop, thread)
