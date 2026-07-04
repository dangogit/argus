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


def test_exhausted_qa_sandbox_network_check_records_environment_blocker(conn, cfg_project):
    cfg_project.team("dev").pipeline.max_iters = 0
    rid = _open(conn, cfg_project, text="update PM summary process"); conn.commit()
    pm_memory.append(conn, team_id="dev", fingerprint="prior",
                     finding="old lesson", outcome="qa-pass")

    _finish_stage(conn, "developer", {"parsed": {}, "memory_fingerprints": ["prior"]})
    pipeline.on_job_done(conn, cfg_project, _reload_last(conn)); conn.commit()
    qa = _finish_stage(conn, "qa", {
        "parsed": {
            "verdict": "fail",
            "summary": (
                "Local Postgres and PyPI checks are blocked by sandbox "
                "networking: pypi.org name resolution failed."
            ),
        },
    })
    pipeline.on_job_done(conn, cfg_project, _reload(conn, qa.id)); conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT outcome, note FROM pm_lessons WHERE team_id='dev' ORDER BY created_at"
        )
        outcome, note = cur.fetchall()[-1]
        cur.execute("SELECT lesson_fingerprint, outcome FROM pm_lesson_attributions")
        attributions = cur.fetchall()
    assert outcome == "blocked"
    assert note.startswith("Environment blocker:")
    assert "PyPI checks are blocked by sandbox networking" in note
    assert attributions == []


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
