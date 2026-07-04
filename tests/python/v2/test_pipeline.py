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


def test_enqueue_stage_sets_prompt_hash_in_snapshot(conn, cfg):
    """Enqueue must freeze a deterministic prompt_hash into exec_snapshot so a
    later output regression can be correlated with a prompt/rules/skills edit."""
    eid = _event(conn, cfg); conn.commit()
    rid = pipeline.open_request(conn, cfg, event_id=eid, team_id="dev",
                                conversation_id=None)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT exec_snapshot FROM jobs WHERE request_id=%s AND stage=0", (rid,))
        snap = cur.fetchone()[0]
    assert isinstance(snap.get("prompt_hash"), str)
    assert len(snap["prompt_hash"]) == 12


def test_enqueue_stage_sets_checkpoint_guidance_in_snapshot(conn, cfg):
    eid = _event(conn, cfg); conn.commit()
    rid = pipeline.open_request(conn, cfg, event_id=eid, team_id="dev",
                                conversation_id=None)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT exec_snapshot FROM jobs WHERE request_id=%s AND stage=0", (rid,))
        snap = cur.fetchone()[0]
    assert "CHECKPOINTS:" in snap.get("checkpoints", "")
    assert "external side effects" in snap["checkpoints"]


def test_add_prompt_hash_is_deterministic():
    a = {"prompt": "do the thing", "rules": "rule block", "skills": "skill block"}
    b = {"prompt": "do the thing", "rules": "rule block", "skills": "skill block"}
    pipeline._add_prompt_hash(a)
    pipeline._add_prompt_hash(b)
    assert a["prompt_hash"] == b["prompt_hash"]
    assert len(a["prompt_hash"]) == 12


def test_add_prompt_hash_changes_with_prompt():
    base = {"prompt": "do the thing", "rules": "rule block", "skills": "skill block"}
    edited = {"prompt": "do a different thing", "rules": "rule block", "skills": "skill block"}
    pipeline._add_prompt_hash(base)
    pipeline._add_prompt_hash(edited)
    assert base["prompt_hash"] != edited["prompt_hash"]


def test_add_prompt_hash_changes_with_rules():
    base = {"prompt": "do the thing", "rules": "rule block", "skills": "skill block"}
    edited = {"prompt": "do the thing", "rules": "different rule block", "skills": "skill block"}
    pipeline._add_prompt_hash(base)
    pipeline._add_prompt_hash(edited)
    assert base["prompt_hash"] != edited["prompt_hash"]


def test_add_prompt_hash_changes_with_skills():
    base = {"prompt": "do the thing", "rules": "rule block", "skills": "skill block"}
    edited = {"prompt": "do the thing", "rules": "rule block", "skills": "different skill block"}
    pipeline._add_prompt_hash(base)
    pipeline._add_prompt_hash(edited)
    assert base["prompt_hash"] != edited["prompt_hash"]


def test_add_prompt_hash_changes_with_checkpoints():
    base = {"prompt": "do the thing", "checkpoints": "CHECKPOINTS:\n- start"}
    edited = {"prompt": "do the thing", "checkpoints": "CHECKPOINTS:\n- different"}
    pipeline._add_prompt_hash(base)
    pipeline._add_prompt_hash(edited)
    assert base["prompt_hash"] != edited["prompt_hash"]


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
        # Simulate a worker mid-run on one sibling: it must still be cancelled,
        # and the worker's later fenced-CAS write-back cannot resurrect it.
        cur.execute(
            "UPDATE jobs SET status='running' WHERE id = ("
            "  SELECT id FROM jobs WHERE request_id=%s AND status='pending' LIMIT 1)",
            (rid,))
    conn.commit()

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


def _developer_job(conn, rid, result: dict) -> Job:
    """Mark the open request's stage-0 developer job done with `result`, then
    return a Job matching that row so on_job_done can advance the pipeline."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, event_id, conversation_id, team_id FROM jobs "
                    "WHERE request_id=%s AND role='developer' AND stage=0", (rid,))
        jid, eid, conv, team_id = cur.fetchone()
        cur.execute("UPDATE jobs SET status='done', result=%s WHERE id=%s",
                    (Json(result), jid))
    return Job(id=jid, request_id=rid, event_id=eid, conversation_id=conv,
               team_id=team_id, role="developer", stage=0, kind="pipeline",
               status="done", attempts=0, max_attempts=3, claim_token=None,
               exec_snapshot={}, payload={})


def _pipeline_job(conn, rid, role: str, stage: int, result: dict) -> Job:
    with conn.cursor() as cur:
        cur.execute("SELECT id, event_id, conversation_id, team_id FROM jobs "
                    "WHERE request_id=%s AND role=%s AND stage=%s", (rid, role, stage))
        jid, eid, conv, team_id = cur.fetchone()
        cur.execute("UPDATE jobs SET status='done', result=%s WHERE id=%s",
                    (Json(result), jid))
    return Job(id=jid, request_id=rid, event_id=eid, conversation_id=conv,
               team_id=team_id, role=role, stage=stage, kind="pipeline",
               status="done", attempts=0, max_attempts=3, claim_token=None,
               exec_snapshot={}, payload={})


def test_recommends_fix_helper_classifies_diagnosed_but_unapplied():
    """_recommends_fix: a concrete, located, recommended fix with no diff is the
    third class of ready=false (not blocked, not genuine no-fix)."""
    analysis = ("A focused code fix is warranted: require amount > 0 before "
                "submit. [components/Pay.vue:498] [server/index.ts:374]")
    assert pipeline._recommends_fix({"ready": False}, analysis) is True
    # Genuine no-fix: no fix markers, no file ref -> False.
    assert pipeline._recommends_fix(
        {"ready": False}, "Reviewed the code; current behavior is correct.") is False
    # Fix language but no file/line reference -> too vague, stay conservative.
    assert pipeline._recommends_fix(
        {"ready": False}, "A fix is warranted somewhere in the codebase.") is False
    # Blocked text must NOT read as recommends-fix (blocked takes precedence).
    assert pipeline._recommends_fix(
        {"status": "blocked"}, "could not access repo [a.ts:1]") is False


def test_dev_recommends_fix_redispatches_developer_under_budget(conn, cfg_project):
    """ready=false + a concrete located fix + no diff, under rework budget:
    re-dispatch the developer (loop back), do NOT close as no-fix."""
    eid = events.ingest_message(conn, cfg_project, team="dev", source="cli",
                                dedup_key="rf1", text="amount can go negative")
    rid = pipeline.open_request(conn, cfg_project, event_id=eid, team_id="dev",
                                conversation_id=None)
    conn.commit()
    analysis = ("A focused code fix is warranted: require amount > 0. "
                "[components/Pay.vue:498] [server/index.ts:374]")
    job = _developer_job(conn, rid, {"has_diff": False,
                                     "parsed": {"ready": False, "analysis": analysis}})
    pipeline.on_job_done(conn, cfg_project, job)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] == "open"  # not closed
        cur.execute("SELECT branch_counters FROM requests WHERE id=%s", (rid,))
        assert (cur.fetchone()[0] or {}).get("0") == 1  # developer rework bumped
        cur.execute("SELECT count(*) FROM jobs WHERE request_id=%s AND role='developer'", (rid,))
        assert cur.fetchone()[0] == 2  # a fresh developer job was enqueued
        cur.execute("SELECT count(*) FROM actions WHERE idempotency_key=%s", (f"nofix:{rid}",))
        assert cur.fetchone()[0] == 0  # NOT closed as no-fix


def test_dev_recommends_fix_redispatch_seeds_prior_analysis(conn, cfg_project):
    """The re-dispatched developer job carries the prior analysis in its brief so
    it applies the fix instead of re-investigating from scratch."""
    eid = events.ingest_message(conn, cfg_project, team="dev", source="cli",
                                dedup_key="rf-seed", text="amount can go negative")
    rid = pipeline.open_request(conn, cfg_project, event_id=eid, team_id="dev",
                                conversation_id=None)
    conn.commit()
    analysis = ("A focused code fix is warranted: require amount > 0. [Pay.vue:498]")
    job = _developer_job(conn, rid, {"has_diff": False,
                                     "parsed": {"ready": False, "analysis": analysis}})
    pipeline.on_job_done(conn, cfg_project, job)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT payload->>'text' FROM jobs WHERE request_id=%s "
                    "AND role='developer' ORDER BY created_at DESC LIMIT 1", (rid,))
        text = cur.fetchone()[0]
    assert "Apply" in text or "apply" in text
    assert "require amount > 0" in text  # prior analysis seeded into the brief


def test_dev_recommends_fix_surfaces_when_budget_exhausted(conn, cfg_project):
    """ready=false + recommends fix + no diff, with the developer rework budget
    already exhausted: reach a DISTINCT 'found, not fixed' terminal outcome with a
    WARN alert, never the clean 'no fix needed' close."""
    e1 = events.ingest_signal(conn, cfg_project, team="dev", source="sentry",
                              fingerprint="RF-EXH", payload={"err": "amount<0"})
    conn.commit()
    rid = pipeline.open_request(conn, cfg_project, event_id=e1, team_id="dev",
                                conversation_id=None, fingerprint="RF-EXH")
    conn.commit()
    # Exhaust the developer rework budget (max_iters=2 in cfg_project).
    with conn.cursor() as cur:
        cur.execute("UPDATE requests SET branch_counters=%s WHERE id=%s",
                    (Json({"0": 2}), rid))
    conn.commit()
    analysis = ("Root cause: missing guard. A focused code fix is warranted: "
                "require amount > 0. [server/index.ts:374]")
    job = _developer_job(conn, rid, {"has_diff": False,
                                     "parsed": {"ready": False, "analysis": analysis}})
    pipeline.on_job_done(conn, cfg_project, job)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] == "failed"  # distinct terminal, not done/no-fix
        cur.execute("SELECT count(*) FROM jobs WHERE request_id=%s AND role='developer'", (rid,))
        assert cur.fetchone()[0] == 1  # no further re-dispatch
        cur.execute("SELECT severity, message FROM alerts WHERE project='dev'")
        sev, msg = cur.fetchone()
        cur.execute("SELECT outcome, note FROM pm_lessons WHERE fingerprint='RF-EXH'")
        outcome, note = cur.fetchone()
    assert sev == "warn"  # warn, not info
    assert "no fix was applied" in msg.lower() or "not applied" in msg.lower()
    assert "no change needed" not in msg.lower()
    assert outcome == "found-not-fixed"
    assert note.startswith("Next repair action: apply the diagnosed fix.")


def test_memory_outcome_note_requires_repair_action_for_known_root_cause_failure():
    note = pipeline._memory_outcome_note(
        "qa-fail",
        "Senior review rejected it. Root cause still present in checkout.py.",
    )

    assert note.startswith("Next repair action: address the review failure root cause.")
    assert "Evidence: Senior review rejected it." in note


def test_memory_outcome_note_requires_repair_action_for_known_root_cause_no_change():
    note = pipeline._memory_outcome_note(
        "no-change",
        "Root cause is stale fixture data in checkout.py.",
    )

    assert note.startswith("Next repair action: address the known root cause.")
    assert "Evidence: Root cause is stale fixture data" in note


def test_memory_outcome_note_preserves_existing_blocking_marker_for_known_root_cause():
    note = pipeline._memory_outcome_note(
        "qa-fail",
        "senior did not pass. Blocking issue: Root cause still present in checkout.py.",
    )

    assert note == (
        "senior did not pass. Blocking issue: Root cause still present in checkout.py."
    )


def test_dev_genuine_no_fix_unchanged(conn, cfg_project):
    """Regression guard: ready=false with no fix markers stays a clean no-fix
    close (info alert / NOFIX), unchanged behavior."""
    e1 = events.ingest_signal(conn, cfg_project, team="dev", source="sentry",
                              fingerprint="NOFIX-1", payload={"err": "noise"})
    conn.commit()
    rid = pipeline.open_request(conn, cfg_project, event_id=e1, team_id="dev",
                                conversation_id=None, fingerprint="NOFIX-1")
    conn.commit()
    job = _developer_job(conn, rid, {
        "has_diff": False,
        "parsed": {"ready": False,
                   "analysis": "Reviewed the code; current behavior is correct."}})
    pipeline.on_job_done(conn, cfg_project, job)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] == "done"
        cur.execute("SELECT severity, message FROM alerts WHERE project='dev'")
        sev, msg = cur.fetchone()
    assert sev == "info"
    assert "no fix needed" in msg.lower()


def test_dev_blocked_unchanged(conn, cfg_project):
    """Regression guard: ready=false with blocked markers stays the blocked close
    (warn alert, FAILED), unchanged; blocked takes precedence over recommends-fix."""
    e1 = events.ingest_signal(conn, cfg_project, team="dev", source="sentry",
                              fingerprint="BLK-1", payload={"err": "403"})
    conn.commit()
    rid = pipeline.open_request(conn, cfg_project, event_id=e1, team_id="dev",
                                conversation_id=None, fingerprint="BLK-1")
    conn.commit()
    job = _developer_job(conn, rid, {
        "has_diff": False,
        "parsed": {"ready": False,
                   "analysis": "Could not access GitHub, connector returns 404."}})
    pipeline.on_job_done(conn, cfg_project, job)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] == "done"
        cur.execute("SELECT count(*) FROM jobs WHERE request_id=%s AND role='developer'", (rid,))
        assert cur.fetchone()[0] == 1  # no re-dispatch for a blocked run
        cur.execute("SELECT severity, message FROM alerts WHERE project='dev'")
        sev, msg = cur.fetchone()
        cur.execute("SELECT outcome, note FROM pm_lessons WHERE fingerprint='BLK-1'")
        outcome, note = cur.fetchone()
    assert sev == "warn"
    assert "blocked" in msg.lower()
    assert outcome == "blocked"
    assert note.startswith("Blocking issue: Could not access GitHub")


def test_dev_ready_advances_to_qa(conn, cfg_project):
    """Regression guard: ready=true advances to qa (stage 1)."""
    eid = events.ingest_message(conn, cfg_project, team="dev", source="cli",
                                dedup_key="ready1", text="fix it")
    rid = pipeline.open_request(conn, cfg_project, event_id=eid, team_id="dev",
                                conversation_id=None)
    conn.commit()
    job = _developer_job(conn, rid, {"has_diff": True,
                                     "parsed": {"ready": True, "summary": "fixed"}})
    pipeline.on_job_done(conn, cfg_project, job)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT role, stage FROM jobs WHERE request_id=%s AND role='qa'", (rid,))
        assert cur.fetchone() == ("qa", 1)


def test_qa_environment_blocker_classifies_postgres_unavailable():
    reason = pipeline._qa_environment_blocker({
        "parsed": {"verdict": "pass"},
        "test_exit": 2,
        "test_output": (
            "psycopg.OperationalError: could not connect to server: "
            "Connection refused. Is the server running on host localhost? "
            "Postgres verification did not run."
        ),
    })

    assert "Postgres verification did not run" in reason


def test_qa_postgres_environment_blocker_is_not_full_pass(conn, cfg_project):
    eid = events.ingest_message(conn, cfg_project, team="dev", source="cli",
                                dedup_key="qa-db-block", text="fix it")
    rid = pipeline.open_request(conn, cfg_project, event_id=eid, team_id="dev",
                                conversation_id=None, fingerprint="QA-DB-BLOCK")
    conn.commit()
    dev = _developer_job(conn, rid, {"has_diff": True,
                                     "parsed": {"ready": True, "summary": "fixed"}})
    pipeline.on_job_done(conn, cfg_project, dev)
    conn.commit()

    qa = _pipeline_job(conn, rid, "qa", 1, {
        "parsed": {"verdict": "pass", "summary": "Looks good."},
        "test_exit": 2,
        "test_output": (
            "psycopg.OperationalError: connection refused\n"
            "Is the server running on host \"127.0.0.1\" and accepting TCP/IP "
            "connections?\nPostgres verification did not run."
        ),
    })
    pipeline.on_job_done(conn, cfg_project, qa)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] == "failed"
        cur.execute("SELECT count(*) FROM actions WHERE request_id=%s AND type='open_pr'", (rid,))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT outcome, note FROM pm_lessons WHERE fingerprint='QA-DB-BLOCK'")
        outcome, note = cur.fetchone()
    assert outcome == "blocked"
    assert "QA environment blocker" in note
    assert "Postgres" in note


def test_failure_text_warns_timeout_may_have_side_effects(conn, cfg_project):
    """A Codex timeout can happen after external work landed, so the owner
    should inspect live state before blindly rerunning."""
    eid = events.ingest_message(conn, cfg_project, team="dev", source="cli",
                                dedup_key="timeout-alert", text="ship prod fix")
    rid = pipeline.open_request(conn, cfg_project, event_id=eid, team_id="dev",
                                conversation_id=None)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status='failed', result=%s "
            "WHERE request_id=%s AND role='developer'",
            (Json({"error": "codex: process timed out after 900s"}), rid),
        )
    text = pipeline._failure_text(conn, rid, "Pipeline job failed before completion.")
    assert "stopped before normal completion" in text
    assert "without opening a PR" not in text
    assert "inspect live state" in text
    assert "developer: codex: process timed out after 900s" in text


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


def test_recommends_fix_heuristic_no_false_positives():
    """_recommends_fix must fire on a real diagnosed-but-unapplied fix and stay
    quiet on genuine no-fix / negated text (review hardening)."""
    from argus.v2.orchestrator.pipeline import _recommends_fix
    real = ("A focused code fix is warranted: require amount > 0 before treating "
            "a payment as paid. FinalPaymentStep.vue:498 payment-process/index.ts:374")
    assert _recommends_fix({"ready": False}, real) is True
    for neg in (
        "No root cause found in auth.ts; behavior is correct.",
        "Nothing should be changed in payment.vue, logic is fine",
        "The error in index.ts is expected and needs to be ignored",
        "should be correct after the recent change in Pay.vue",
    ):
        assert _recommends_fix({"ready": False}, neg) is False
    # blocked + explicit no_fix status both short-circuit regardless of text
    assert _recommends_fix({"status": "blocked"}, real) is False
    assert _recommends_fix({"status": "no_fix"}, real) is False


def test_found_not_fixed_close_cancels_sibling_jobs(conn, cfg):
    """_found_not_fixed_close marks the request failed AND cancels non-terminal
    sibling jobs (same invariant as _fail), so none zombie as 'pending'."""
    eid = _event(conn, cfg); conn.commit()
    rid = pipeline.open_request(conn, cfg, event_id=eid, team_id="dev",
                                conversation_id=None); conn.commit()
    pipeline.enqueue_stage(conn, cfg, request_id=rid, stage_index=1); conn.commit()
    pipeline._found_not_fixed_close(conn, cfg, rid,
                                    "Found root cause in x.py:1, fix is warranted")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] == "failed"
        cur.execute("SELECT count(*) FROM jobs WHERE request_id=%s "
                    "AND status IN ('pending','claimed','running')", (rid,))
        assert cur.fetchone()[0] == 0
