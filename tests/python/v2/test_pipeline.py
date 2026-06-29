from psycopg.types.json import Json

from argus.v2.orchestrator import pipeline
from argus.v2.ingress import events
from argus.v2.config import loader
from argus.v2.queue import jobs as qjobs
from argus.v2.queue.models import Job
from argus.v2.workspace import repo as workspace


def _event(conn, cfg):
    return events.ingest_message(conn, cfg, team="dev", source="cli",
                                 dedup_key="m1", text="fix login")


def test_open_request_creates_first_stage_job(conn, cfg):
    eid = _event(conn, cfg); conn.commit()
    rid = pipeline.open_request(conn, cfg, event_id=eid, team_id="dev",
                                conversation_id=None)
    conn.commit()
    assert rid is not None
    with conn.cursor() as cur:
        cur.execute("SELECT role, stage, kind FROM jobs WHERE request_id=%s", (rid,))
        rows = cur.fetchall()
    assert rows == [("developer", 0, "pipeline")]  # first stage only


def test_enqueue_stage_sets_project_in_snapshot(conn, cfg):
    """Developer/pipeline jobs must carry project=team_id so the worker sets
    ARGUS_PROJECT (hermes per-project profile). Regression: it was omitted, so
    pipeline roles ran on the _default profile."""
    eid = _event(conn, cfg); conn.commit()
    rid = pipeline.open_request(conn, cfg, event_id=eid, team_id="dev",
                                conversation_id=None)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT exec_snapshot FROM jobs WHERE request_id=%s AND stage=0", (rid,))
        snap = cur.fetchone()[0]
    assert snap.get("project") == "dev"


def test_no_fix_close_routes_to_conversation_channel(conn, cfg):
    """The no-fix/blocked close must address the conversation's real channel
    (whatsapp:<jid>), not a bare 'conv:<uuid>' that deliver() drops. Regression:
    the owner never saw "blocked"/"no fix" because it routed nowhere."""
    eid = events.ingest_message(conn, cfg, team="dev", source="cli", dedup_key="cv1",
                                text="inspect PRs", conversation_key="whatsapp:grp9")
    conn.commit()
    rid = pipeline.open_request(conn, cfg, event_id=eid, team_id="dev",
                                conversation_id=_conv_id(conn, eid))
    conn.commit()
    pipeline._no_fix_close(conn, cfg, rid, "could not access GitHub, 404", blocked=True)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT destination_ref, payload->>'text' FROM actions "
                    "WHERE idempotency_key=%s", (f"nofix:{rid}",))
        dest, text = cur.fetchone()
    assert dest == "whatsapp:grp9"          # routable, not conv:<uuid>
    assert text.startswith("Blocked, couldn't complete the task:")


def _conv_id(conn, event_id):
    with conn.cursor() as cur:
        cur.execute("SELECT conversation_id FROM events WHERE id=%s", (event_id,))
        return cur.fetchone()[0]


def test_looks_blocked_distinguishes_block_from_no_fix():
    """ready=false means 'blocked' (no access) vs 'no fix needed' (looked, fine).
    Regression: a GitHub-blocked dev run was reported as 'no fix needed'."""
    blocked = "I could not access GitHub, connector returns 404 for the repo."
    assert pipeline._looks_blocked({}, blocked) is True
    assert pipeline._looks_blocked({"status": "blocked"}, "anything") is True
    assert pipeline._looks_blocked({}, "Reviewed the code; current behavior is correct.") is False
    # Explicit no_fix status wins even if analysis text trips a marker word.
    assert pipeline._looks_blocked({"status": "no_fix"}, "could not find a bug") is False


def test_enqueue_stage_is_idempotent(conn, cfg):
    eid = _event(conn, cfg); conn.commit()
    rid = pipeline.open_request(conn, cfg, event_id=eid, team_id="dev",
                                conversation_id=None)
    conn.commit()
    pipeline.enqueue_stage(conn, cfg, request_id=rid, stage_index=0)  # duplicate
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs WHERE request_id=%s AND stage=0", (rid,))
        assert cur.fetchone()[0] == 1


def test_signal_fingerprint_dedups_open_request(conn, cfg):
    e1 = events.ingest_signal(conn, cfg, team="dev", source="sentry",
                              fingerprint="ISSUE-1", payload={"err": "boom"})
    conn.commit()
    r1 = pipeline.open_request(conn, cfg, event_id=e1, team_id="dev",
                               conversation_id=None, fingerprint="ISSUE-1")
    conn.commit()
    e2 = events.ingest_signal(conn, cfg, team="dev", source="sentry",
                              fingerprint="ISSUE-1", payload={"err": "boom again"})
    conn.commit()
    r2 = pipeline.open_request(conn, cfg, event_id=e2, team_id="dev",
                               conversation_id=None, fingerprint="ISSUE-1")
    conn.commit()
    assert r1 is not None and r2 is None  # deduped while r1 is open


def test_signal_fingerprint_dedups_across_completion(conn, cfg):
    # A standing connector row keeps re-emitting the same fingerprint. Once a
    # request for it has completed, a re-emission must NOT open a second pipeline
    # (dedup_terminal). Otherwise each poll spawns another dev job for the same
    # row (the propwise runaway).
    e1 = events.ingest_signal(conn, cfg, team="dev", source="sb",
                              fingerprint="ROW-1", payload={"err": "boom"})
    conn.commit()
    r1 = pipeline.open_request(conn, cfg, event_id=e1, team_id="dev",
                               conversation_id=None, fingerprint="ROW-1",
                               dedup_terminal=True)
    assert r1 is not None
    with conn.cursor() as cur:
        cur.execute("UPDATE requests SET status='done' WHERE id=%s", (r1,))
    conn.commit()

    e2 = events.ingest_signal(conn, cfg, team="dev", source="sb-2",
                              fingerprint="ROW-1", payload={"err": "boom again"})
    conn.commit()
    r2 = pipeline.open_request(conn, cfg, event_id=e2, team_id="dev",
                               conversation_id=None, fingerprint="ROW-1",
                               dedup_terminal=True)
    conn.commit()
    assert r2 is None  # already turned into a request once; no second pipeline
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs WHERE team_id='dev' AND stage=0")
        assert cur.fetchone()[0] == 1  # exactly one stage-0 job for the row


def test_triage_jobs_dedup_on_fingerprint(conn, cfg):
    # The triage job key is fingerprint-derived, so two emissions of the same
    # signal collapse to a single job via ON CONFLICT.
    e1 = events.ingest_signal(conn, cfg, team="dev", source="sb",
                              fingerprint="ROW-9", payload={"err": "x"})
    conn.commit()
    j1 = pipeline.enqueue_triage(conn, cfg, event_id=e1, team_id="dev",
                                 fingerprint="ROW-9")
    j2 = pipeline.enqueue_triage(conn, cfg, event_id=e1, team_id="dev",
                                 fingerprint="ROW-9")
    conn.commit()
    assert j1 == j2  # same idempotency_key -> same job
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs WHERE kind='triage' AND team_id='dev'")
        assert cur.fetchone()[0] == 1


def test_triage_ignore_notifies_control_channel(conn, tmp_path):
    y = tmp_path / "triage.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n  - name: dev\n"
        "    roles: [ { name: manager, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [manager] }\n"
        "    channels: [ { type: fake, role: control, channel_id: chat } ]\n",
        encoding="utf-8",
    )
    cfg = loader.load(y)
    jid = qjobs.enqueue(
        conn,
        team_id="dev",
        kind="triage",
        role="manager",
        stage=0,
        idempotency_key="triage-ignore",
        exec_snapshot={},
        payload={"fingerprint": "ISSUE-1"},
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status='done', result=%s WHERE id=%s",
            (Json({"parsed": {"action": "ignore", "reply": "Already handled."}}), jid),
        )
    job = Job(id=jid, request_id=None, event_id=None, conversation_id=None,
              team_id="dev", role="manager", stage=0, kind="triage",
              status="done", attempts=0, max_attempts=3, claim_token=None,
              exec_snapshot={}, payload={"fingerprint": "ISSUE-1"})

    pipeline._handle_triage(conn, cfg, job)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, destination_ref, payload->>'text', payload->>'triage' "
            "FROM actions WHERE idempotency_key=%s",
            (f"triage:{jid}",),
        )
        status, dest, text, triage = cur.fetchone()
    assert status == "proposed"
    assert dest == "fake:chat"
    assert text == "Already handled."
    assert triage == "ignore"


def test_fail_cancels_sibling_jobs(conn, cfg):
    """_fail marks the request failed AND cancels its non-terminal sibling jobs.
    Regression: it only updated the request, leaving sibling jobs stuck
    'pending' forever (on_job_done early-returns once the request isn't 'open',
    and jobs.claim filters only on status='pending'), so they became zombies
    that never advance. They must end up 'dead', not 'pending'."""
    eid = _event(conn, cfg)
    conn.commit()
    rid = pipeline.open_request(conn, cfg, event_id=eid, team_id="dev",
                                conversation_id=None)
    conn.commit()
    # Add a second stage job so there is a sibling to orphan.
    pipeline.enqueue_stage(conn, cfg, request_id=rid, stage_index=1)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs WHERE request_id=%s AND status='pending'",
                    (rid,))
        assert cur.fetchone()[0] == 2  # developer + qa both pending

    pipeline._fail(conn, cfg, rid, "build failed")
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] == "failed"
        # No sibling job is left lingering in a non-terminal state.
        cur.execute(
            "SELECT count(*) FROM jobs WHERE request_id=%s "
            "AND status IN ('pending','claimed','running')", (rid,))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM jobs WHERE request_id=%s AND status='dead'",
                    (rid,))
        assert cur.fetchone()[0] == 2


def test_pr_summary_uses_builder_llm_summary(conn, cfg, tmp_path):
    eid = events.ingest_message(conn, cfg, team="dev", source="cli",
                                dedup_key="sum1", text="cannot save profile")
    rid = pipeline.open_request(conn, cfg, event_id=eid, team_id="dev",
                                conversation_id=None)
    conn.commit()
    note = "Save button POSTed to the wrong route; pointed it at /api/profile."
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET result=%s WHERE request_id=%s AND role='developer'",
            (Json({"has_diff": True,
                   "parsed": {"ready": True, "summary": note}}), rid))
    conn.commit()

    assert pipeline._builder_summary(conn, rid) == note
    info = pipeline._pr_info(conn, cfg, rid, cwd=str(tmp_path))
    assert info["summary_short"] == note
    # Summary is the LLM line, not a mechanical filename list.
    summary_section = info["body"].split("## Changed Files")[0]
    assert note in summary_section
    assert "file(s):" not in summary_section


def test_pr_summary_falls_back_to_request_text(conn, cfg, tmp_path):
    eid = events.ingest_message(conn, cfg, team="dev", source="cli",
                                dedup_key="sum2", text="fix the thing")
    rid = pipeline.open_request(conn, cfg, event_id=eid, team_id="dev",
                                conversation_id=None)
    conn.commit()
    # No builder summary recorded -> plain request text, never filenames.
    assert pipeline._builder_summary(conn, rid) == ""
    info = pipeline._pr_info(conn, cfg, rid, cwd=str(tmp_path))
    assert info["summary_short"] == "fix the thing"
    assert "file(s):" not in info["body"].split("## Changed Files")[0]


def test_critical_diff_scan_blocks_open_pr(conn, cfg_project, monkeypatch, tmp_path):
    eid = events.ingest_message(conn, cfg_project, team="dev", source="cli",
                                dedup_key="scan-block", text="fix")
    rid = pipeline.open_request(conn, cfg_project, event_id=eid, team_id="dev",
                                conversation_id=None)
    conn.commit()

    monkeypatch.setattr(workspace, "_wt_path", lambda request_id: tmp_path)
    monkeypatch.setattr(workspace, "diff",
                        lambda project, cwd: "+token = 'sk-newsecretnewsecretnewsecret'\n")

    job = Job(id="job1", request_id=rid, event_id=eid, conversation_id=None,
              team_id="dev", role="senior", stage=2, kind="pipeline",
              status="done", attempts=0, max_attempts=3, claim_token=None,
              exec_snapshot={}, payload={})
    pipeline._approve_done(conn, cfg_project, job)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] == "failed"
        cur.execute("SELECT count(*) FROM actions WHERE type='open_pr'")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT payload->>'text' FROM actions WHERE type='notify'")
        assert "Deterministic diff scan blocked PR" in cur.fetchone()[0]
