from psycopg.types.json import Json

from argus.v2.orchestrator import pipeline
from argus.v2.ingress import events
from argus.v2.pm import memory as pm_memory
from argus.v2.queue import jobs
from argus.v2.queue.models import RunRecord
from argus.v2.workspace import repo as workspace


def _open(conn, cfg, text="fix login"):
    eid = events.ingest_message(conn, cfg, team="dev", source="cli", dedup_key="m1", text=text)
    return pipeline.open_request(conn, cfg, event_id=eid, team_id="dev", conversation_id=None)


def _finish_stage(conn, role, result):
    job = jobs.claim(conn, "w"); conn.commit()
    assert job.role == role
    jobs.finalize(conn, job.id, job.claim_token, status="done", result=result,
                  run=RunRecord(role=role, engine="scripted", status="ok"), actions=[])
    conn.commit()
    return job


def test_qa_fail_loops_back_to_developer(conn, cfg_project):
    rid = _open(conn, cfg_project); conn.commit()
    _finish_stage(conn, "developer", {"output": "", "parsed": {}})
    pipeline.on_job_done(conn, cfg_project, _reload_last(conn)); conn.commit()
    job = _finish_stage(conn, "qa", {"parsed": {"verdict": "fail"}})
    pipeline.on_job_done(conn, cfg_project, _reload(conn, job.id)); conn.commit()
    # Next claimable job is developer again (a retry), not senior.
    nxt = jobs.claim(conn, "w"); conn.commit()
    assert nxt.role == "developer"


def test_rework_enqueues_fresh_qa_iteration(conn, cfg_project):
    rid = _open(conn, cfg_project); conn.commit()
    _finish_stage(conn, "developer", {"output": "", "parsed": {}})
    pipeline.on_job_done(conn, cfg_project, _reload_last(conn)); conn.commit()
    qa = _finish_stage(conn, "qa", {"parsed": {"verdict": "fail"}})
    pipeline.on_job_done(conn, cfg_project, _reload(conn, qa.id)); conn.commit()

    rework = _finish_stage(conn, "developer", {"output": "", "parsed": {}})
    pipeline.on_job_done(conn, cfg_project, _reload(conn, rework.id)); conn.commit()

    nxt = jobs.claim(conn, "w"); conn.commit()
    assert nxt.role == "qa"
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs WHERE request_id=%s AND role='qa'", (rid,))
        assert cur.fetchone()[0] == 2


def test_exhausted_qa_records_project_memory(conn, cfg_project):
    cfg_project.team("dev").pipeline.max_iters = 0
    rid = _open(conn, cfg_project, text="fix payment"); conn.commit()
    pm_memory.append(conn, team_id="dev", fingerprint="prior",
                     finding="old lesson", outcome="qa-pass")

    _finish_stage(conn, "developer", {"parsed": {}, "memory_fingerprints": ["prior"]})
    pipeline.on_job_done(conn, cfg_project, _reload_last(conn)); conn.commit()
    qa = _finish_stage(conn, "qa", {"parsed": {"verdict": "fail"}})
    pipeline.on_job_done(conn, cfg_project, _reload(conn, qa.id)); conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] == "failed"
        cur.execute(
            "SELECT finding, outcome FROM pm_lessons WHERE team_id='dev' ORDER BY created_at"
        )
        assert cur.fetchall()[-1] == ("fix payment", "qa-fail")
        cur.execute("SELECT lesson_fingerprint, outcome FROM pm_lesson_attributions")
        assert cur.fetchall() == [("prior", "qa-fail")]


def test_exhausted_senior_records_blocking_detail(conn, cfg_project):
    cfg_project.team("dev").pipeline.max_iters = 0
    rid = _open(conn, cfg_project, text="fix checkout total"); conn.commit()

    _finish_stage(conn, "developer", {"parsed": {}})
    pipeline.on_job_done(conn, cfg_project, _reload_last(conn)); conn.commit()
    qa = _finish_stage(conn, "qa", {"parsed": {"verdict": "pass"}})
    pipeline.on_job_done(conn, cfg_project, _reload(conn, qa.id)); conn.commit()
    senior = _finish_stage(conn, "senior", {
        "parsed": {
            "decision": "reject",
            "reason": "Root cause still present in checkout.py: missing total guard.",
        }
    })
    pipeline.on_job_done(conn, cfg_project, _reload(conn, senior.id)); conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] == "failed"
        cur.execute("SELECT outcome, note FROM pm_lessons WHERE team_id='dev'")
        outcome, note = cur.fetchone()
    assert outcome == "qa-fail"
    assert "Blocking issue: Root cause still present in checkout.py" in note


def test_stale_senior_reject_reconciles_against_latest_passes(conn, cfg_project):
    cfg_project.team("dev").pipeline.max_iters = 0
    rid = _open(conn, cfg_project, text="fix status reconciliation"); conn.commit()

    _finish_stage(conn, "developer", {"parsed": {}})
    pipeline.on_job_done(conn, cfg_project, _reload_last(conn)); conn.commit()
    qa = _finish_stage(conn, "qa", {"parsed": {"verdict": "pass"}})
    pipeline.on_job_done(conn, cfg_project, _reload(conn, qa.id)); conn.commit()
    stale_senior = _finish_stage(conn, "senior", {
        "parsed": {"decision": "reject", "reason": "stale reject"}
    })
    with conn.cursor() as cur:
        cur.execute("SELECT event_id FROM requests WHERE id=%s", (rid,))
        event_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO jobs
              (request_id, event_id, team_id, role, stage, kind, status,
               idempotency_key, result, updated_at)
            VALUES
              (%s,%s,'dev','senior',2,'pipeline','done',
               'fresh-senior-approve',%s,clock_timestamp())
            """,
            (rid, event_id, Json({"parsed": {"decision": "approve"}})),
        )
    conn.commit()

    pipeline.on_job_done(conn, cfg_project, _reload(conn, stale_senior.id)); conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] == "done"
        cur.execute("SELECT outcome, note FROM pm_lessons WHERE team_id='dev'")
        outcome, note = cur.fetchone()
    assert outcome == "qa-pass"
    assert note == "Latest QA passed and latest senior approved."


def test_review_memory_recording_reconciles_stale_qa_fail(conn, cfg_project):
    rid = _open(
        conn,
        cfg_project,
        text="retro-change b1a990d7 converse b25a21be reconcile status",
    )
    conn.commit()

    _finish_stage(conn, "developer", {"parsed": {}})
    pipeline.on_job_done(conn, cfg_project, _reload_last(conn)); conn.commit()
    qa = _finish_stage(conn, "qa", {"parsed": {"verdict": "pass"}})
    pipeline.on_job_done(conn, cfg_project, _reload(conn, qa.id)); conn.commit()
    _finish_stage(conn, "senior", {"parsed": {"decision": "approve"}})

    outcome, note = pipeline._record_memory_outcome(
        conn,
        rid,
        "qa-fail",
        "stale converse evidence recorded qa-fail after latest approval",
        team=cfg_project.team("dev"),
        reconcile_review_status=True,
    )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT outcome, note FROM pm_lessons WHERE team_id='dev'")
        recorded = cur.fetchone()

    assert (outcome, note) == ("qa-pass", "Latest QA passed and latest senior approved.")
    assert recorded == ("qa-pass", "Latest QA passed and latest senior approved.")


def test_failure_draft_pr_risk_reconciles_stale_status(conn, cfg_project, monkeypatch, tmp_path):
    cfg_project.team("dev").pipeline.max_iters = 0
    rid = _open(
        conn,
        cfg_project,
        text="retro-change b1a990d7 converse b25a21be stale PR risk",
    )
    conn.commit()

    monkeypatch.setattr(workspace, "_wt_path", lambda request_id: tmp_path)
    monkeypatch.setattr(workspace, "diff", lambda project, cwd: "+fixed\n")
    monkeypatch.setattr(pipeline, "_changed_files", lambda cwd: ["src/status.py"])

    _finish_stage(conn, "developer", {"has_diff": True, "parsed": {}})
    pipeline.on_job_done(conn, cfg_project, _reload_last(conn)); conn.commit()
    qa = _finish_stage(conn, "qa", {"parsed": {"verdict": "pass"}})
    pipeline.on_job_done(conn, cfg_project, _reload(conn, qa.id)); conn.commit()
    stale_senior = _finish_stage(conn, "senior", {
        "parsed": {"decision": "reject", "reason": "stale status"}
    })
    with conn.cursor() as cur:
        cur.execute("SELECT event_id FROM requests WHERE id=%s", (rid,))
        event_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO jobs
              (request_id, event_id, team_id, role, stage, kind, status,
               idempotency_key, result, updated_at)
            VALUES
              (%s,%s,'dev','senior',2,'pipeline','done',
               'fresh-senior-approve-for-risk',%s,clock_timestamp())
            """,
            (rid, event_id, Json({"parsed": {"decision": "approve"}})),
        )
    conn.commit()

    assert pipeline._open_draft_pr_after_failure(
        conn,
        cfg_project,
        _reload(conn, stale_senior.id),
        "stale converse evidence recorded qa-fail after latest approval",
    )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] == "done"
        cur.execute("SELECT outcome, note FROM pm_lessons WHERE team_id='dev'")
        assert cur.fetchone() == (
            "qa-pass",
            "Latest QA passed and latest senior approved.",
        )
        cur.execute("SELECT count(*) FROM actions WHERE type='open_pr'")
        assert cur.fetchone()[0] == 0


def test_exhausted_qa_with_diff_opens_draft_pr(conn, cfg_project, monkeypatch, tmp_path):
    cfg_project.team("dev").pipeline.max_iters = 0
    rid = _open(conn, cfg_project, text="fix OpenClaw email lookup"); conn.commit()

    monkeypatch.setattr(workspace, "_wt_path", lambda request_id: tmp_path)
    monkeypatch.setattr(workspace, "diff", lambda project, cwd: "+fixed\n")
    monkeypatch.setattr(pipeline, "_changed_files", lambda cwd: ["src/email.ts"])

    _finish_stage(conn, "developer", {
        "has_diff": True,
        "parsed": {"ready": True, "summary": "Added email lookup support."},
    })
    pipeline.on_job_done(conn, cfg_project, _reload_last(conn)); conn.commit()
    qa = _finish_stage(conn, "qa", {"parsed": {"verdict": "fail"}})
    pipeline.on_job_done(conn, cfg_project, _reload(conn, qa.id)); conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] == "done"
        cur.execute("SELECT payload FROM actions WHERE type='open_pr'")
        payload = cur.fetchone()[0]
        assert payload["draft"] is True
        assert payload["changed_files"] == ["src/email.ts"]
        assert payload["checks"] == "QA: fail"
        assert "needs review" in payload["risk_summary"]
        assert "QA failed" in payload["body"]


def test_qa_pass_then_senior_approve_completes(conn, cfg_project):
    rid = _open(conn, cfg_project); conn.commit()
    _finish_stage(conn, "developer", {"output": "", "parsed": {}})
    pipeline.on_job_done(conn, cfg_project, _reload_last(conn)); conn.commit()
    qa = _finish_stage(conn, "qa", {"parsed": {"verdict": "pass"}})
    pipeline.on_job_done(conn, cfg_project, _reload(conn, qa.id)); conn.commit()
    sr = _finish_stage(conn, "senior", {"parsed": {"decision": "approve"}})
    pipeline.on_job_done(conn, cfg_project, _reload(conn, sr.id)); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] in ("done", "awaiting_approval")  # done, or gated on PR-merge


# helpers
def _reload(conn, jid):
    from argus.v2.orchestrator.reconcile import _load_job
    return _load_job(conn, jid)


def _reload_last(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM jobs ORDER BY updated_at DESC LIMIT 1")
        return _reload(conn, str(cur.fetchone()[0]))
