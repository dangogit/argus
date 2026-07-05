"""Full live e2e: a chat message drives developer (scripted file edit) -> qa
(test_cmd 'true' -> pass) -> senior (scripted approve) -> a ready PR opened
via the executor with a fake gh runner. Uses a real temp git repo (no network)
and ARGUS_RUN_ROOT under tmp_path.

Deviations from the plan (corrected here, asserting the same guarantees):
1. The plan queries `WHERE type='pr'` but the action is inserted with
   type='open_pr' (per _approve_done). Fixed to `WHERE type='open_pr'`.
2. reconcile.sweep_once internally calls executor.process_proposed with no
   runner (would invoke the real gh binary). The test patches sweep_once to
   skip its executor step and calls executor.process_proposed manually with
   the fake runner so the PR is opened via the injected runner only.
"""
import subprocess
from pathlib import Path

import pytest

from argus.v2.config import loader
from argus.v2.ingress import events
from argus.v2.orchestrator import reconcile, pipeline
from argus.v2.actions import executor
from argus.v2.worker import worker


@pytest.fixture()
def project_cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_RUN_ROOT", str(tmp_path / "run"))
    r = tmp_path / "proj"
    r.mkdir()

    def g(*a):
        subprocess.run(["git", *a], cwd=r, check=True, capture_output=True)

    g("init", "-b", "main")
    g("config", "user.email", "t@t.co")
    g("config", "user.name", "t")
    (r / "README.md").write_text("x\n")
    g("add", "-A")
    g("commit", "-m", "init")

    y = tmp_path / "a.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: scripted } }\n"
        "teams:\n  - name: dev\n"
        f"    project: {{ repo: {r}, base_branch: main,"
        " work_branch_prefix: argus/dev, test_cmd: 'true' }\n"
        "    roles:\n"
        "      - { name: developer, kind: builder, prompt: p,"
        " engine: { engine: scripted, model: null } }\n"
        "      - { name: qa, kind: judge, prompt: p }\n"
        "      - { name: senior, kind: judge, prompt: p }\n"
        "    pipeline: { stages: [developer, qa, senior], max_iters: 2 }\n"
    )
    return loader.load(y), str(r)


def test_message_drives_dev_qa_senior_then_opens_ready_pr(
    conn, project_cfg, monkeypatch
):
    cfg, _repo = project_cfg

    # Inject scripted output per role via _role_snapshot_extra seam.
    # developer: writes fix.py and signals ready.
    # senior: approves.
    # qa: driven by test_cmd='true' (exit 0 -> pass); output irrelevant.
    def role_extra(role):
        if role == "developer":
            return {
                "scripted_edit": {"path": "fix.py", "content": "print(1)\n"},
                "scripted_output": 'ARGUS_RESULT: {"ready": true}',
            }
        if role == "senior":
            return {"scripted_output": 'ARGUS_RESULT: {"decision": "approve"}'}
        return {"scripted_output": "ok"}

    monkeypatch.setattr(pipeline, "_role_snapshot_extra", role_extra)

    # Ingest the triggering chat message.
    events.ingest_message(
        conn, cfg, team="dev", source="cli", dedup_key="m1",
        text="fix the login bug",
    )
    conn.commit()

    calls = []

    def fake_runner(argv, cwd=None):
        calls.append(argv)
        if argv[:2] == ["gh", "pr"]:
            return "https://github.com/x/y/pull/1\n"
        return ""

    # Drive the pipeline: sweep routes events and advances stages; worker
    # runs jobs; we call process_proposed with the fake runner so open_pr
    # is executed via the injected runner only (not the real gh binary).
    for _ in range(12):
        # Sweep: route events + reclaim + advance pipeline (skip its executor step).
        reconcile.route_events(conn, cfg)
        conn.commit()
        from argus.v2.queue import jobs as job_queue
        job_queue.reclaim_expired(conn)
        conn.commit()
        # Advance any terminal pipeline jobs.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT j.id FROM jobs j JOIN requests r ON r.id=j.request_id
                WHERE j.kind='pipeline' AND j.status IN ('done','dead')
                  AND r.status='open'
                  AND NOT EXISTS (
                    SELECT 1 FROM jobs n
                    WHERE n.request_id=j.request_id AND n.stage=j.stage+1)
                  AND (j.status='dead' OR j.stage+1 < (
                    SELECT count(*) FROM jobs k WHERE k.request_id=j.request_id) + 5)
                """
            )
            ids = [str(row[0]) for row in cur.fetchall()]
        for jid in ids:
            pipeline.on_job_done(conn, cfg, reconcile._load_job(conn, jid))
        conn.commit()

        # Run workers until the queue is empty.
        while worker.run_once(cfg, "w1"):
            pass

        # Drain the action outbox with the fake runner.
        executor.process_proposed(conn, cfg, runner=fake_runner)
        conn.commit()

    # Request must be done.
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests")
        row = cur.fetchone()
    assert row is not None, "no request was created"
    assert row[0] == "done", f"request status is {row[0]!r}, expected 'done'"

    # The open_pr action must be done with a real (fake) PR URL.
    # NOTE: the action type is 'open_pr', not 'pr' (plan had a typo).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, provider_ref FROM actions WHERE type='open_pr'"
        )
        action_row = cur.fetchone()
    assert action_row is not None, "no open_pr action was recorded"
    st, ref = action_row
    assert st == "done", f"open_pr action status is {st!r}"
    assert ref == "https://github.com/x/y/pull/1", f"provider_ref is {ref!r}"

    # The fake gh runner was actually called with 'gh pr create'.
    assert any(a[:2] == ["gh", "pr"] for a in calls), (
        f"gh pr create was never called; calls={calls}"
    )
    assert not any("--draft" in a for a in calls)

    with conn.cursor() as cur:
        cur.execute("SELECT payload->>'text' FROM actions WHERE type='notify'")
        notices = [row[0] for row in cur.fetchall()]
    assert any("New bug report:" in text and "PR:" in text for text in notices)
