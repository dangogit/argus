from argus.v2.config import loader
from argus.v2.orchestrator import reconcile
from argus.v2.ingress import events
from argus.v2.orchestrator import pipeline
from argus.v2.queue import jobs
from argus.v2.queue.models import RunRecord


def _terminal_request(conn, cfg, tmp_path, *, dedup, age_done=True):
    """Open a request, mark it done, and create a leftover worktree dir.
    age_done backdates updated_at past the prune grace window. Returns the dir."""
    eid = events.ingest_message(conn, cfg, team="dev", source="cli",
                                dedup_key=dedup, text="t"); conn.commit()
    rid = pipeline.open_request(conn, cfg, event_id=eid, team_id="dev",
                               conversation_id=None); conn.commit()
    backdate = " - interval '1 hour'" if age_done else ""
    with conn.cursor() as cur:
        cur.execute(f"UPDATE requests SET status='done', updated_at=now(){backdate} "
                    "WHERE id=%s", (rid,))
    conn.commit()
    wtdir = tmp_path / "worktrees" / rid
    wtdir.mkdir(parents=True)
    return rid, wtdir


def test_sweep_prunes_terminal_worktrees(conn, cfg_project, tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_RUN_ROOT", str(tmp_path))
    _rid, wtdir = _terminal_request(conn, cfg_project, tmp_path, dedup="prune1")
    assert wtdir.exists()
    reconcile.sweep_once(conn, cfg_project); conn.commit()
    assert not wtdir.exists()


def test_sweep_keeps_fresh_terminal_worktree(conn, cfg_project, tmp_path, monkeypatch):
    # A request that only just became terminal is inside the grace window: a
    # zombie engine could still be writing into its worktree, so do not prune.
    monkeypatch.setenv("ARGUS_RUN_ROOT", str(tmp_path))
    _rid, wtdir = _terminal_request(conn, cfg_project, tmp_path, dedup="prune2",
                                    age_done=False)
    reconcile.sweep_once(conn, cfg_project); conn.commit()
    assert wtdir.exists()


def test_sweep_keeps_worktree_with_pending_action(conn, cfg_project, tmp_path, monkeypatch):
    # An aged terminal request whose open_pr action is still awaiting approval
    # must keep its worktree: the action needs it to push once approved.
    monkeypatch.setenv("ARGUS_RUN_ROOT", str(tmp_path))
    rid, wtdir = _terminal_request(conn, cfg_project, tmp_path, dedup="prune3")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO actions (request_id, team_id, type, risk, "
            "idempotency_key, status) "
            "VALUES (%s,'dev','open_pr','reversible_internal',%s,'awaiting_approval')",
            (rid, f"open_pr:{rid}"))
    conn.commit()
    reconcile.sweep_once(conn, cfg_project); conn.commit()
    assert wtdir.exists()


def test_sweep_advances_completed_stage(conn, cfg):
    eid = events.ingest_message(conn, cfg, team="dev", source="cli",
                                dedup_key="m1", text="t"); conn.commit()
    rid = pipeline.open_request(conn, cfg, event_id=eid, team_id="dev",
                                conversation_id=None); conn.commit()
    job = jobs.claim(conn, "w1"); conn.commit()
    jobs.finalize(conn, job.id, job.claim_token, status="done", result={},
                  run=RunRecord(role="developer", engine="echo", status="ok"),
                  actions=[]); conn.commit()
    # Reconcile should enqueue the next stage (qa).
    reconcile.sweep_once(conn, cfg); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT role FROM jobs WHERE request_id=%s ORDER BY stage", (rid,))
        roles = [r[0] for r in cur.fetchall()]
    assert roles == ["developer", "qa"]


def test_sweep_fails_request_for_failed_stage(conn, cfg_project):
    eid = events.ingest_message(conn, cfg_project, team="dev", source="cli",
                                dedup_key="failed-stage", text="t")
    rid = pipeline.open_request(conn, cfg_project, event_id=eid, team_id="dev",
                                conversation_id=None)
    conn.commit()
    job = jobs.claim(conn, "w1")
    conn.commit()
    jobs.finalize(conn, job.id, job.claim_token, status="failed",
                  result={"error": "boom"},
                  run=RunRecord(role="developer", engine="echo", status="failed"),
                  actions=[])
    conn.commit()

    reconcile.sweep_once(conn, cfg_project)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] == "failed"
        cur.execute("SELECT advanced_at IS NOT NULL FROM jobs WHERE id=%s", (job.id,))
        assert cur.fetchone()[0] is True


def test_sweep_marks_unknown_team_terminal_job_failed(conn, cfg):
    eid = events.ingest_message(conn, cfg, team="ghost", source="cli",
                                dedup_key="unknown-team-job", text="t")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO requests (event_id, team_id) VALUES (%s,'ghost') RETURNING id",
            (eid,))
        rid = str(cur.fetchone()[0])
    jobs.enqueue(conn, team_id="ghost", kind="pipeline", role="developer", stage=0,
                 idempotency_key="unknown-team-job", exec_snapshot={},
                 payload={}, request_id=rid, event_id=eid)
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET status='done' WHERE request_id=%s", (rid,))
    conn.commit()

    reconcile.sweep_once(conn, cfg)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] == "failed"
        cur.execute("SELECT advanced_at IS NOT NULL FROM jobs WHERE request_id=%s", (rid,))
        assert cur.fetchone()[0] is True
        cur.execute("SELECT status FROM events WHERE id=%s", (eid,))
        assert cur.fetchone()[0] == "processed"


def test_sweep_marks_unknown_team_async_job_skipped(conn, cfg):
    eid = events.ingest_signal(conn, cfg, team="ghost", source="supabase",
                               fingerprint="unknown-team-triage",
                               payload={"message": "bug"})
    jid = jobs.enqueue(conn, team_id="ghost", kind="triage", role="manager", stage=0,
                       idempotency_key="unknown-team-triage-job",
                       exec_snapshot={}, payload={}, event_id=eid)
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET status='done' WHERE id=%s", (jid,))
    conn.commit()

    reconcile.sweep_once(conn, cfg)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, payload->>'skipped' FROM actions "
            "WHERE idempotency_key='triage:' || %s::uuid",
            (jid,),
        )
        assert cur.fetchone() == ("done", "missing_team")


def test_sweep_advances_loopback_developer_once(conn, cfg_project):
    eid = events.ingest_message(conn, cfg_project, team="dev", source="cli",
                                dedup_key="m-loop", text="fix it"); conn.commit()
    rid = pipeline.open_request(conn, cfg_project, event_id=eid, team_id="dev",
                                conversation_id=None); conn.commit()
    with conn.cursor() as cur:
        cur.execute("UPDATE events SET status='processed', processed_at=now() WHERE id=%s", (eid,))
    conn.commit()

    dev = jobs.claim(conn, "w1"); conn.commit()
    jobs.finalize(conn, dev.id, dev.claim_token, status="done",
                  result={"parsed": {"ready": True}},
                  run=RunRecord(role="developer", engine="echo", status="ok"),
                  actions=[]); conn.commit()
    reconcile.sweep_once(conn, cfg_project); conn.commit()

    qa = jobs.claim(conn, "w1"); conn.commit()
    assert qa.role == "qa"
    jobs.finalize(conn, qa.id, qa.claim_token, status="done",
                  result={"parsed": {"verdict": "fail"}},
                  run=RunRecord(role="qa", engine="echo", status="ok"),
                  actions=[]); conn.commit()
    reconcile.sweep_once(conn, cfg_project); conn.commit()

    retry = jobs.claim(conn, "w1"); conn.commit()
    assert retry.role == "developer"
    jobs.finalize(conn, retry.id, retry.claim_token, status="done",
                  result={"parsed": {"ready": False, "analysis": "already handled"}},
                  run=RunRecord(role="developer", engine="echo", status="ok"),
                  actions=[]); conn.commit()
    reconcile.sweep_once(conn, cfg_project); conn.commit()
    reconcile.sweep_once(conn, cfg_project); conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] == "done"
        cur.execute("SELECT role, status FROM jobs WHERE request_id=%s ORDER BY created_at", (rid,))
        assert cur.fetchall() == [
            ("developer", "done"),
            ("qa", "done"),
            ("developer", "done"),
        ]


def test_sweep_reclaims_expired(conn, cfg):
    jobs.enqueue(conn, team_id="dev", kind="pipeline", role="developer", stage=0,
                 idempotency_key="k", exec_snapshot={}, payload={}); conn.commit()
    job = jobs.claim(conn, "w1"); conn.commit()
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET lease_expires_at=now()-interval '1 min' WHERE id=%s", (job.id,))
    conn.commit()
    reconcile.sweep_once(conn, cfg); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM jobs WHERE id=%s", (job.id,))
        assert cur.fetchone()[0] == "pending"


def test_sweep_prunes_orphan_worktree_without_request_row(conn, cfg_project, tmp_path,
                                                          monkeypatch):
    """A worktree dir whose owning request row was deleted is never matched by the
    terminal-request prune, so it must be GC'd as an orphan once aged past grace."""
    import os
    import time as _time
    monkeypatch.setenv("ARGUS_RUN_ROOT", str(tmp_path))
    orphan = tmp_path / "worktrees" / "deadbeef-no-such-request"
    orphan.mkdir(parents=True)
    old = _time.time() - 2 * 3600
    os.utime(orphan, (old, old))
    reconcile.sweep_once(conn, cfg_project); conn.commit()
    assert not orphan.exists()


def test_sweep_keeps_fresh_orphan_worktree(conn, cfg_project, tmp_path, monkeypatch):
    """A brand-new dir with no request row yet (dir created before the row commits)
    is inside the grace window, so it must NOT be reaped."""
    monkeypatch.setenv("ARGUS_RUN_ROOT", str(tmp_path))
    fresh = tmp_path / "worktrees" / "00000000-fresh-orphan"
    fresh.mkdir(parents=True)
    reconcile.sweep_once(conn, cfg_project); conn.commit()
    assert fresh.exists()


def _cfg_with_control_channel(tmp_path):
    y = tmp_path / "dead-job.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n  - name: dev\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "    channels: [ { type: fake, role: control, channel_id: chat } ]\n",
        encoding="utf-8",
    )
    return loader.load(y)


def test_sweep_notifies_dead_job(conn, tmp_path):
    cfg = _cfg_with_control_channel(tmp_path)
    jobs.enqueue(conn, team_id="dev", kind="pipeline", role="developer", stage=0,
                idempotency_key="dead-1", exec_snapshot={}, payload={}, max_attempts=1)
    conn.commit()
    job = jobs.claim(conn, "w1"); conn.commit()
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET lease_expires_at=now() - interval '1 min' WHERE id=%s",
                    (job.id,))
    conn.commit()

    reconcile.sweep_once(conn, cfg); conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM jobs WHERE id=%s", (job.id,))
        assert cur.fetchone()[0] == "dead"
        cur.execute(
            "SELECT destination_ref, payload->>'text' FROM actions "
            "WHERE idempotency_key=%s", (f"dead-job:{job.id}",))
        row = cur.fetchone()
    assert row is not None
    dest, text = row
    assert dest == "fake:chat"
    assert job.id in text
    assert "kind=pipeline" in text
    assert "team=dev" in text


def test_sweep_dead_job_notify_is_idempotent_across_double_sweep(conn, tmp_path):
    cfg = _cfg_with_control_channel(tmp_path)
    jobs.enqueue(conn, team_id="dev", kind="pipeline", role="developer", stage=0,
                idempotency_key="dead-2", exec_snapshot={}, payload={}, max_attempts=1)
    conn.commit()
    job = jobs.claim(conn, "w1"); conn.commit()
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET lease_expires_at=now() - interval '1 min' WHERE id=%s",
                    (job.id,))
    conn.commit()

    reconcile.sweep_once(conn, cfg); conn.commit()
    reconcile.sweep_once(conn, cfg); conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM actions WHERE idempotency_key=%s",
                    (f"dead-job:{job.id}",))
        assert cur.fetchone()[0] == 1
