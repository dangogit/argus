from psycopg.types.json import Json

from argus.v2.orchestrator import pipeline
from argus.v2.ingress import events
from argus.v2.pm import memory as pm_memory
from argus.v2.queue import jobs
from argus.v2.queue.models import RunRecord
from argus.v2.workspace import repo as workspace


def _open(conn, cfg, text="fix login", fingerprint=None):
    eid = events.ingest_message(conn, cfg, team="dev", source="cli", dedup_key="m1", text=text)
    return pipeline.open_request(conn, cfg, event_id=eid, team_id="dev",
                                 conversation_id=None, fingerprint=fingerprint)


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
            "SELECT finding, outcome, note FROM pm_lessons "
            "WHERE team_id='dev' ORDER BY created_at"
        )
        finding, outcome, note = cur.fetchall()[-1]
        assert (finding, outcome) == ("fix payment", "qa-fail")
        assert "Failure classification: unknown" in note
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
        assert "QA: fail" in payload["checks"]
        assert "Failure classification: unknown" in payload["checks"]
        assert "needs review" in payload["risk_summary"]
        assert "failure classification: unknown" in payload["risk_summary"]
        assert "QA failed" in payload["body"]


def test_exhausted_qa_reconciles_latest_pass_before_recording_failure(
        conn, cfg_project, monkeypatch, tmp_path):
    cfg_project.team("dev").pipeline.max_iters = 0
    rid = _open(
        conn, cfg_project,
        text=("Implement minimal Argus process change so qa-fail lesson and PR-risk "
              "recording first reconciles against the latest QA and senior run statuses."),
        fingerprint="converse:61f70886-9a28-4607-b2b4-953bb404b4cf",
    )
    conn.commit()

    monkeypatch.setattr(workspace, "_wt_path", lambda request_id: tmp_path)
    monkeypatch.setattr(workspace, "diff", lambda project, cwd: "+fixed\n")
    monkeypatch.setattr(pipeline, "_changed_files", lambda cwd: ["src/pipeline.py"])

    _finish_stage(conn, "developer", {"has_diff": True, "parsed": {"ready": True}})
    pipeline.on_job_done(conn, cfg_project, _reload_last(conn)); conn.commit()
    stale_qa = jobs.claim(conn, "w"); conn.commit()
    assert stale_qa.role == "qa"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO jobs "
            "(request_id,event_id,team_id,kind,role,stage,status,result,idempotency_key) "
            "VALUES (%s,%s,'dev','pipeline','qa',1,'done',%s,'qa-latest-pass'), "
            "(%s,%s,'dev','pipeline','senior',2,'done',%s,'senior-latest-approve')",
            (
                rid, stale_qa.event_id, Json({"parsed": {"verdict": "pass"}}),
                rid, stale_qa.event_id, Json({"parsed": {"decision": "approve"}}),
            ),
        )
    conn.commit()
    jobs.finalize(conn, stale_qa.id, stale_qa.claim_token, status="done",
                  result={"parsed": {"verdict": "fail"}},
                  run=RunRecord(role="qa", engine="scripted", status="ok"), actions=[])
    conn.commit()

    pipeline.on_job_done(conn, cfg_project, _reload(conn, stale_qa.id)); conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT outcome FROM pm_lessons WHERE fingerprint=%s",
                    ("converse:61f70886-9a28-4607-b2b4-953bb404b4cf",))
        assert cur.fetchall() == []
        cur.execute("SELECT payload->>'risk_summary' FROM actions WHERE type='open_pr'")
        assert cur.fetchall() == []


def test_exhausted_senior_reconciles_latest_approval_before_retro_change_failure(
        conn, cfg_project):
    cfg_project.team("dev").pipeline.max_iters = 0
    rid = _open(
        conn, cfg_project,
        text=("Implement minimal internal process change so PR risk and lesson outcome "
              "recording reconciles against the latest QA and senior statuses."),
        fingerprint="retro-change:032fdae8db0a6c18282199e7",
    )
    conn.commit()

    _finish_stage(conn, "developer", {"parsed": {}})
    pipeline.on_job_done(conn, cfg_project, _reload_last(conn)); conn.commit()
    qa = _finish_stage(conn, "qa", {"parsed": {"verdict": "pass"}})
    pipeline.on_job_done(conn, cfg_project, _reload(conn, qa.id)); conn.commit()
    stale_senior = jobs.claim(conn, "w"); conn.commit()
    assert stale_senior.role == "senior"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO jobs "
            "(request_id,event_id,team_id,kind,role,stage,status,result,idempotency_key) "
            "VALUES (%s,%s,'dev','pipeline','senior',2,'done',%s,'senior-latest-pass')",
            (rid, stale_senior.event_id, Json({"parsed": {"decision": "approve"}})),
        )
    conn.commit()
    jobs.finalize(conn, stale_senior.id, stale_senior.claim_token, status="done",
                  result={"parsed": {"decision": "reject"}},
                  run=RunRecord(role="senior", engine="scripted", status="ok"), actions=[])
    conn.commit()

    pipeline.on_job_done(conn, cfg_project, _reload(conn, stale_senior.id)); conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT outcome FROM pm_lessons WHERE fingerprint=%s",
                    ("retro-change:032fdae8db0a6c18282199e7",))
        assert cur.fetchall() == []


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
