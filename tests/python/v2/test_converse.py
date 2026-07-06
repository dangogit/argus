"""C3-C5: converse job end-to-end tests. All use the scripted engine so no
network is needed. Drive each ARGUS_RESULT branch and verify idempotency."""
from pathlib import Path

import pytest

from argus.v2.config import loader
from argus.v2.ingress import events
from argus.v2.orchestrator import pipeline, reconcile
from argus.v2.worker import worker


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def cfg_converse(tmp_path):
    """Config with a manager role that has a scripted engine (conversational
    front enabled) plus the normal dev pipeline."""
    y = tmp_path / "c.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n  - name: dev\n"
        "    roles:\n"
        "      - name: manager\n"
        "        kind: front\n"
        "        prompt: 'You are the manager.'\n"
        "        engine: { engine: scripted }\n"
        "      - { name: developer, kind: builder, prompt: p }\n"
        "      - { name: qa, kind: judge, prompt: p }\n"
        "      - { name: senior, kind: judge, prompt: p }\n"
        "    pipeline: { stages: [developer, qa, senior], max_iters: 2 }\n"
        "    channels:\n"
        "      - { type: cli, role: control, channel_id: local }\n"
    )
    return loader.load(y)


def _ingest(conn, cfg, *, text="what are the open PRs?", key="cm1",
            conv_key="whatsapp:grp1"):
    return events.ingest_message(
        conn, cfg, team="dev", source="cli", dedup_key=key,
        text=text, conversation_key=conv_key)


def _converse_result(action, reply="", task=""):
    import json
    return f'ARGUS_RESULT: {json.dumps({"action": action, "reply": reply, "task": task})}'


# ---------------------------------------------------------------------------
# C3: enqueue_converse + route_events
# ---------------------------------------------------------------------------

def test_route_events_enqueues_converse_job_when_manager_has_engine(
        conn, cfg_converse, monkeypatch):
    """route_events produces a converse job (not a pipeline request) when the
    manager role has an engine configured."""
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": _converse_result("ignore")})
    eid = _ingest(conn, cfg_converse); conn.commit()
    reconcile.route_events(conn, cfg_converse); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT kind, role, idempotency_key FROM jobs WHERE team_id='dev'")
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "converse"
    assert row[1] == "manager"
    assert row[2] == f"converse:{eid}"


def test_route_events_posts_immediate_ack_on_converse(conn, cfg_converse, monkeypatch):
    """The instant a human message is routed to a converse job, an ack reply is
    posted to the conversation channel so the chat shows Argus is on it (instead
    of going silent while the manager engine runs)."""
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": _converse_result("ignore")})
    eid = _ingest(conn, cfg_converse); conn.commit()
    reconcile.route_events(conn, cfg_converse); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT destination_ref, payload->>'text' FROM actions "
                    "WHERE idempotency_key=%s AND type='reply'", (f"ack:{eid}",))
        row = cur.fetchone()
    assert row is not None, "no immediate ack action was posted"
    assert row[0] == "whatsapp:grp1"
    assert "looking into this" in row[1].lower()


def test_route_events_ack_idempotent(conn, cfg_converse, monkeypatch):
    """Re-routing the same event does not post a duplicate ack."""
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": _converse_result("ignore")})
    eid = _ingest(conn, cfg_converse); conn.commit()
    reconcile.route_events(conn, cfg_converse); conn.commit()
    with conn.cursor() as cur:
        cur.execute("UPDATE events SET status='received', processed_at=NULL WHERE id=%s", (eid,))
    conn.commit()
    reconcile.route_events(conn, cfg_converse); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM actions WHERE idempotency_key=%s", (f"ack:{eid}",))
        assert cur.fetchone()[0] == 1


def test_route_events_no_ack_without_routable_channel(conn, cfg_converse, monkeypatch):
    """A converse on a cli-only conversation (no routable channel) posts no ack."""
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": _converse_result("ignore")})
    eid = _ingest(conn, cfg_converse, key="cli1", conv_key="cli:local"); conn.commit()
    reconcile.route_events(conn, cfg_converse); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM actions WHERE idempotency_key=%s", (f"ack:{eid}",))
        assert cur.fetchone()[0] == 0


def test_converse_snapshot_uses_team_memory_profile(conn, cfg_converse, monkeypatch):
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": _converse_result("ignore")})
    _ingest(conn, cfg_converse); conn.commit()
    reconcile.route_events(conn, cfg_converse); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT exec_snapshot->>'project' FROM jobs WHERE kind='converse'")
        assert cur.fetchone()[0] == "dev"


def test_route_events_converse_idempotent(conn, cfg_converse, monkeypatch):
    """Running route_events twice for the same event enqueues only one job."""
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": _converse_result("ignore")})
    eid = _ingest(conn, cfg_converse); conn.commit()
    # Reset status to received so we can run route_events twice.
    reconcile.route_events(conn, cfg_converse); conn.commit()
    with conn.cursor() as cur:
        cur.execute("UPDATE events SET status='received', processed_at=NULL WHERE id=%s",
                    (eid,))
    conn.commit()
    reconcile.route_events(conn, cfg_converse); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs WHERE idempotency_key=%s",
                    (f"converse:{eid}",))
        assert cur.fetchone()[0] == 1


def test_route_events_no_engine_falls_back_to_rule(conn, cfg):
    """When the manager role has no engine, the rule-based path still works
    (regression: reply for non-work messages, dispatch for work messages)."""
    # Non-work message: reply action, no request.
    events.ingest_message(conn, cfg, team="dev", source="cli",
                          dedup_key="noe1", text="thanks!"); conn.commit()
    reconcile.route_events(conn, cfg); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM requests"); assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM actions WHERE type='reply'")
        assert cur.fetchone()[0] == 1


def test_route_events_no_engine_dispatch_for_work_message(conn, cfg):
    """When the manager role has no engine, a work verb opens a pipeline request."""
    events.ingest_message(conn, cfg, team="dev", source="cli",
                          dedup_key="noe2", text="fix the login bug"); conn.commit()
    reconcile.route_events(conn, cfg); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM requests"); assert cur.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# C4: worker injects manager_state for converse jobs
# ---------------------------------------------------------------------------

def test_worker_converse_job_includes_manager_state_in_prompt(
        conn, cfg_converse, monkeypatch):
    """The scripted engine captures the full prompt; verify manager_state is
    appended (the state block header appears in the run output's prompt)."""
    captured = {}

    def fake_manager_state(conn, cfg, team_id):
        captured["called"] = True
        return "MANAGER_STATE_SENTINEL"

    # Patch manager_state before the worker runs.
    from argus.v2.front import front as front_mod
    monkeypatch.setattr(front_mod, "manager_state", fake_manager_state)
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": _converse_result("ignore")})

    _ingest(conn, cfg_converse); conn.commit()
    reconcile.route_events(conn, cfg_converse); conn.commit()
    worker.run_once(cfg_converse, "w1"); conn.commit()

    # Confirm manager_state was called.
    assert captured.get("called"), "manager_state was not called during converse job"

    # Confirm the run prompt includes the sentinel.
    with conn.cursor() as cur:
        cur.execute("SELECT prompt FROM runs WHERE role='manager'")
        row = cur.fetchone()
    assert row is not None, "no run row for manager"
    assert "MANAGER_STATE_SENTINEL" in row[0]


# ---------------------------------------------------------------------------
# C5: on_job_done answer branch
# ---------------------------------------------------------------------------

def test_converse_answer_inserts_reply_action(conn, cfg_converse, monkeypatch):
    """answer -> a reply action to the conversation channel_ref."""
    reply_text = "There are 3 open PRs."
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": _converse_result("answer",
                                                                        reply=reply_text)})
    eid = _ingest(conn, cfg_converse); conn.commit()

    # Drive the full converse cycle.
    for _ in range(4):
        reconcile.route_events(conn, cfg_converse); conn.commit()
        while worker.run_once(cfg_converse, "w1"):
            pass
        reconcile.sweep_once(conn, cfg_converse); conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT payload, idempotency_key FROM actions WHERE type='reply'")
        rows = cur.fetchall()
    assert len(rows) >= 1
    texts = [r[0].get("text", "") for r in rows]
    assert any(reply_text in t for t in texts)
    # Idempotency key has the converse:<job_id> form.
    keys = [r[1] for r in rows]
    assert any(k.startswith("converse:") for k in keys)


def test_converse_answer_idempotent(conn, cfg_converse, monkeypatch):
    """Re-running sweep after an answer does not insert a duplicate reply."""
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": _converse_result(
                            "answer", reply="PRs: none.")})
    _ingest(conn, cfg_converse); conn.commit()
    for _ in range(4):
        reconcile.route_events(conn, cfg_converse); conn.commit()
        while worker.run_once(cfg_converse, "w1"):
            pass
        reconcile.sweep_once(conn, cfg_converse); conn.commit()
    # Extra sweeps must not add duplicate reply actions.
    reconcile.sweep_once(conn, cfg_converse); conn.commit()
    reconcile.sweep_once(conn, cfg_converse); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM actions WHERE type='reply' "
                    "AND idempotency_key LIKE 'converse:%%'")
        assert cur.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# C5: on_job_done dispatch branch
# ---------------------------------------------------------------------------

def test_converse_dispatch_opens_pipeline_request_and_ack(conn, cfg_converse, monkeypatch):
    """dispatch -> a pipeline request (fingerprint converse:<job_id>) + ack reply."""
    task_text = "Fix the login page redirect."
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": _converse_result(
                            "dispatch", reply="On it!", task=task_text)})
    _ingest(conn, cfg_converse); conn.commit()
    for _ in range(4):
        reconcile.route_events(conn, cfg_converse); conn.commit()
        while worker.run_once(cfg_converse, "w1"):
            pass
        reconcile.sweep_once(conn, cfg_converse); conn.commit()

    with conn.cursor() as cur:
        # A pipeline request must exist.
        cur.execute("SELECT id, fingerprint FROM requests")
        rows = cur.fetchall()
    assert len(rows) == 1
    fp = rows[0][1]
    assert fp is not None and fp.startswith("converse:")

    # Ack reply must exist keyed converse:<job_id>.
    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM actions WHERE type='reply' "
                    "AND idempotency_key LIKE 'converse:%%'")
        reply_rows = cur.fetchall()
    assert any("On it!" in r[0].get("text", "") for r in reply_rows)


def test_converse_dispatch_idempotent(conn, cfg_converse, monkeypatch):
    """Re-sweep does not open a duplicate pipeline request."""
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": _converse_result(
                            "dispatch", reply="On it!", task="Fix login.")})
    _ingest(conn, cfg_converse); conn.commit()
    for _ in range(4):
        reconcile.route_events(conn, cfg_converse); conn.commit()
        while worker.run_once(cfg_converse, "w1"):
            pass
        reconcile.sweep_once(conn, cfg_converse); conn.commit()
    reconcile.sweep_once(conn, cfg_converse); conn.commit()
    reconcile.sweep_once(conn, cfg_converse); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM requests")
        assert cur.fetchone()[0] == 1


def test_converse_dispatch_dedups_equivalent_task_text(conn, cfg_converse, monkeypatch):
    task = "Deduplicate equivalent retro and converse work before PM dispatch"
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": _converse_result(
                            "dispatch", reply="On it!", task=task)})

    _ingest(conn, cfg_converse, key="dedupe-1"); conn.commit()
    for _ in range(4):
        reconcile.route_events(conn, cfg_converse); conn.commit()
        while worker.run_once(cfg_converse, "w1"):
            pass
        reconcile.sweep_once(conn, cfg_converse); conn.commit()

    _ingest(conn, cfg_converse, key="dedupe-2"); conn.commit()
    for _ in range(4):
        reconcile.route_events(conn, cfg_converse); conn.commit()
        while worker.run_once(cfg_converse, "w1"):
            pass
        reconcile.sweep_once(conn, cfg_converse); conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM requests")
        assert cur.fetchone()[0] == 1
        cur.execute(
            "SELECT count(*) FROM actions "
            "WHERE type='reply' AND idempotency_key LIKE 'converse:%%'"
        )
        assert cur.fetchone()[0] == 2


def test_converse_dispatch_vague_task_asks_for_detail(conn, cfg_converse, monkeypatch):
    """A degenerate dispatch task ('fix') opens NO request; it replies asking
    for specifics instead of building a junk PR (owner-reported 2026-06-19)."""
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": _converse_result(
                            "dispatch", reply="", task="fix")})
    _ingest(conn, cfg_converse); conn.commit()
    for _ in range(4):
        reconcile.route_events(conn, cfg_converse); conn.commit()
        while worker.run_once(cfg_converse, "w1"):
            pass
        reconcile.sweep_once(conn, cfg_converse); conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM requests")
        assert cur.fetchone()[0] == 0  # no junk pipeline request
        cur.execute("SELECT payload FROM actions WHERE type='reply' "
                    "AND idempotency_key LIKE 'converse:%%'")
        replies = cur.fetchall()
    assert any("more detail" in r[0].get("text", "") for r in replies)


def test_converse_dispatch_collapses_doubled_task(conn, cfg_converse, monkeypatch):
    """A doubled dispatch task opens a request whose text is de-duplicated."""
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": _converse_result(
                            "dispatch", reply="On it!",
                            task="fix login redirect fix login redirect")})
    _ingest(conn, cfg_converse); conn.commit()
    for _ in range(4):
        reconcile.route_events(conn, cfg_converse); conn.commit()
        while worker.run_once(cfg_converse, "w1"):
            pass
        reconcile.sweep_once(conn, cfg_converse); conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM requests")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT e.payload->>'text' FROM events e "
                    "JOIN requests r ON r.event_id = e.id")
        assert cur.fetchone()[0] == "fix login redirect"  # collapsed, not doubled


# ---------------------------------------------------------------------------
# C5: on_job_done ignore branch
# ---------------------------------------------------------------------------

def test_converse_ignore_acks_owner_no_dispatch(conn, cfg_converse, monkeypatch):
    """ignore -> still acks the owner with a reply (no silent drop), no request.
    With no manager reply text it falls back to a short ack."""
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": _converse_result("ignore")})
    _ingest(conn, cfg_converse); conn.commit()
    for _ in range(4):
        reconcile.route_events(conn, cfg_converse); conn.commit()
        while worker.run_once(cfg_converse, "w1"):
            pass
        reconcile.sweep_once(conn, cfg_converse); conn.commit()

    with conn.cursor() as cur:
        # Owner gets an ack reply keyed converse:<job>.
        cur.execute("SELECT type, payload FROM actions WHERE idempotency_key LIKE 'converse:%%'")
        row = cur.fetchone()
        # No pipeline request.
        cur.execute("SELECT count(*) FROM requests")
        no_req = cur.fetchone()[0]
    assert row is not None
    assert row[0] == "reply"
    assert row[1].get("text") == "Got it."
    assert no_req == 0


def test_converse_ignore_uses_manager_reply_when_present(conn, cfg_converse, monkeypatch):
    """ignore WITH a reply -> that reply is sent verbatim (manager chose not to
    dispatch but still answered)."""
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": _converse_result(
                            "ignore", reply="Marked as read.")})
    _ingest(conn, cfg_converse); conn.commit()
    for _ in range(4):
        reconcile.route_events(conn, cfg_converse); conn.commit()
        while worker.run_once(cfg_converse, "w1"):
            pass
        reconcile.sweep_once(conn, cfg_converse); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM actions WHERE idempotency_key LIKE 'converse:%%' "
                    "AND type='reply'")
        row = cur.fetchone()
    assert row is not None and row[0].get("text") == "Marked as read."


def test_converse_ignore_idempotent(conn, cfg_converse, monkeypatch):
    """Re-sweep on an ignore does not insert duplicate ack actions."""
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": _converse_result("ignore")})
    _ingest(conn, cfg_converse); conn.commit()
    for _ in range(4):
        reconcile.route_events(conn, cfg_converse); conn.commit()
        while worker.run_once(cfg_converse, "w1"):
            pass
        reconcile.sweep_once(conn, cfg_converse); conn.commit()
    reconcile.sweep_once(conn, cfg_converse); conn.commit()
    reconcile.sweep_once(conn, cfg_converse); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM actions WHERE idempotency_key LIKE 'converse:%%'")
        assert cur.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# C5: garbled ARGUS_RESULT treated as ignore
# ---------------------------------------------------------------------------

def test_converse_garbled_result_treated_as_ignore(conn, cfg_converse, monkeypatch):
    """A garbled ARGUS_RESULT (bad JSON) falls through to ignore: no dispatch,
    but the owner still gets a short ack reply (no silent drop)."""
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output":
                                   "ARGUS_RESULT: {not valid json at all"})
    _ingest(conn, cfg_converse, key="garbled1"); conn.commit()
    for _ in range(4):
        reconcile.route_events(conn, cfg_converse); conn.commit()
        while worker.run_once(cfg_converse, "w1"):
            pass
        reconcile.sweep_once(conn, cfg_converse); conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM requests")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT type, payload FROM actions WHERE idempotency_key LIKE 'converse:%%'")
        row = cur.fetchone()
    assert row is not None and row[0] == "reply"
    assert row[1].get("text") == "Got it."


# ---------------------------------------------------------------------------
# Outage fallback: a failed converse job (engine outage) must not drop the
# message; it falls back to the deterministic rule.
# ---------------------------------------------------------------------------

def test_converse_outage_falls_back_to_rule_dispatch(conn, cfg_converse, monkeypatch):
    """A converse job that ends 'failed' (manager outage) on a work-verb message
    dispatches via the rule fallback, with a single marker reply."""
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": _converse_result("ignore")})
    _ingest(conn, cfg_converse, text="fix the login bug", key="out1"); conn.commit()
    reconcile.route_events(conn, cfg_converse); conn.commit()
    # Simulate the manager engine outage: the converse job is finalized 'failed'.
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET status='failed' WHERE kind='converse'")
    conn.commit()
    reconcile.sweep_once(conn, cfg_converse); conn.commit()
    reconcile.sweep_once(conn, cfg_converse); conn.commit()  # idempotent
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM requests")
        assert cur.fetchone()[0] == 1   # work verb dispatched via fallback
        cur.execute("SELECT count(*) FROM actions WHERE idempotency_key LIKE 'converse:%%'")
        assert cur.fetchone()[0] == 1   # one marker reply, no duplicate


def test_converse_outage_falls_back_to_rule_reply(conn, cfg_converse, monkeypatch):
    """A failed converse job on a non-work message replies (no dispatch), once."""
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": _converse_result("ignore")})
    _ingest(conn, cfg_converse, text="thanks!", key="out2"); conn.commit()
    reconcile.route_events(conn, cfg_converse); conn.commit()
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET status='failed' WHERE kind='converse'")
    conn.commit()
    reconcile.sweep_once(conn, cfg_converse); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM requests")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT type FROM actions WHERE idempotency_key LIKE 'converse:%%'")
        assert cur.fetchone()[0] == "reply"


# ---------------------------------------------------------------------------
# Security: converse turn allowlist + server-side risk override.
# Only explicitly allowlisted manager actions are persisted; all others are
# dropped. Risk is server-overridden so the model cannot self-approve actions.
# ---------------------------------------------------------------------------

def test_converse_allowlist_permits_close_pr_and_runs_it(conn, cfg_converse, monkeypatch):
    """close_pr is in the allowlist and risk_for gives reversible_internal, so
    it should auto-execute (the executor runs it via the mock runner)."""
    # The mock runner records gh calls and returns a canned ref.
    runner_calls = []
    def mock_runner(argv, cwd=None):
        runner_calls.append(argv)
        return f"closed:{argv}\n"

    # Model supplies a DIFFERENT repo ("o/r"); the worker must override it with
    # the team's server-derived repo so the PM cannot touch arbitrary repos.
    scripted = ('ARGUS_RESULT: {"action": "answer", "reply": "ok"}\n'
                'ARGUS_ACTIONS: [{"type": "close_pr", "risk": "irreversible_outward", '
                '"payload": {"number": 73, "repo": "o/r"}}]')
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": scripted})
    from argus.v2.front import front as _front
    monkeypatch.setattr(_front, "_gh_owner_repo", lambda cfg, t: "dangogit/sample-app")

    from argus.v2.actions import executor as _exec
    monkeypatch.setattr(_exec, "_runner_override", mock_runner, raising=False)

    _ingest(conn, cfg_converse, key="cpr1"); conn.commit()
    for _ in range(4):
        reconcile.route_events(conn, cfg_converse); conn.commit()
        while worker.run_once(cfg_converse, "w1"):
            pass
        from argus.v2.actions import executor
        executor.process_proposed(conn, cfg_converse, runner=mock_runner); conn.commit()
        reconcile.sweep_once(conn, cfg_converse); conn.commit()

    with conn.cursor() as cur:
        # The close_pr action must have been persisted (allowlisted).
        cur.execute("SELECT type, risk, status FROM actions WHERE type='close_pr'")
        row = cur.fetchone()
    assert row is not None, "close_pr action was not persisted"
    act_type, risk, status = row
    # Risk must be server-overridden to reversible_internal (NOT the model's irreversible).
    assert risk == "reversible_internal", f"Expected reversible_internal, got {risk}"
    # It should auto-run (reversible_internal, autonomy auto).
    assert status == "done", f"Expected done, got {status}"
    # Runner was called with gh pr close, against the SERVER repo (not the model's "o/r").
    assert any("close" in " ".join(c) for c in runner_calls), "gh pr close not called"
    assert any("dangogit/sample-app" in " ".join(c) for c in runner_calls), \
        "close_pr must target the server-derived repo, not the model's"
    assert not any("o/r" in " ".join(c) for c in runner_calls), \
        "model-supplied repo must be ignored"
    # The persisted payload repo is the server repo.
    with conn.cursor() as cur:
        cur.execute("SELECT payload->>'repo' FROM actions WHERE type='close_pr'")
        assert cur.fetchone()[0] == "dangogit/sample-app"
    # Converse reply also present (the immediate ack is a separate reply keyed ack:).
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM actions WHERE type='reply' "
                    "AND idempotency_key LIKE 'converse:%%'")
        assert cur.fetchone()[0] == 1


def test_converse_email_search_replies_with_result_summary(conn, cfg_converse, monkeypatch):
    from argus.v2.config.schema import SourceRef

    cfg_converse.company.sources.append(SourceRef(
        type="support_apps_script",
        name="support-dev",
        scope="company",
        team="dev",
        secret="k",
        config={"url": "https://example.com/app"},
    ))
    scripted = (
        'ARGUS_RESULT: {"action": "answer", "reply": "Searching support mailbox now."}\n'
        'ARGUS_ACTIONS: [{"type": "email_search", "payload": '
        '{"query": "from:eesha newer_than:30d", "limit": 5}}]'
    )
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": scripted})

    from argus.v2.actions import executor

    def fake_run(action_type, payload, *, runner=None, cfg=None, team_id=None):
        assert action_type == "email_search"
        assert team_id == "dev"
        return (
            '[{"thread_id":"T9","sender":"Eesha Patel <eesha@example.com>",'
            '"subject":"Need help","snippet":"Instance is stuck"}]'
        )

    monkeypatch.setattr(executor._handlers, "run", fake_run)

    _ingest(conn, cfg_converse, key="email1"); conn.commit()
    for _ in range(4):
        reconcile.route_events(conn, cfg_converse); conn.commit()
        while worker.run_once(cfg_converse, "w1"):
            pass
        reconcile.sweep_once(conn, cfg_converse); conn.commit()
        executor.process_proposed(conn, cfg_converse); conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT status, destination_ref FROM actions WHERE type='email_search'")
        assert cur.fetchone() == ("done", "whatsapp:grp1")
        cur.execute("SELECT payload->>'text' FROM actions WHERE type='notify' "
                    "AND idempotency_key LIKE 'email_result:%'")
        text = cur.fetchone()[0]
    assert "Email search: 1 result(s)" in text
    assert "Eesha Patel" in text
    assert "T9" in text


def test_converse_drops_merge_pr_and_deploy(conn, cfg_converse, monkeypatch):
    """merge_pr and deploy are NOT manager-reachable (removed from the allowlist):
    a free-form chat message must never trigger a merge-to-base or an arbitrary
    deploy command. They are dropped; the reply still goes out."""
    scripted = ('ARGUS_RESULT: {"action": "answer", "reply": "ok"}\n'
                'ARGUS_ACTIONS: [{"type": "merge_pr", "payload": {"pr": "73"}}, '
                '{"type": "deploy", "payload": {"command": "rm -rf /"}}]')
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": scripted})
    _ingest(conn, cfg_converse, key="mpr1"); conn.commit()
    for _ in range(4):
        reconcile.route_events(conn, cfg_converse); conn.commit()
        while worker.run_once(cfg_converse, "w1"):
            pass
        from argus.v2.actions import executor
        executor.process_proposed(conn, cfg_converse); conn.commit()
        reconcile.sweep_once(conn, cfg_converse); conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM actions WHERE type IN ('merge_pr','deploy')")
        assert cur.fetchone()[0] == 0, "merge_pr/deploy must not be manager-triggerable"
        cur.execute("SELECT count(*) FROM actions WHERE type='reply' "
                    "AND idempotency_key LIKE 'converse:%%'")
        assert cur.fetchone()[0] == 1


def test_harden_actions_overrides_risk_for_non_converse_jobs(cfg_project):
    """The risk override applies to EVERY job, not just converse: a builder/judge
    job that emits merge_pr tagged reversible must be stored irreversible (so the
    approval gate cannot be skipped from a non-converse code path)."""
    from argus.v2.worker.worker import _harden_actions
    from argus.v2.queue.models import ActionIntent, Job
    job = Job(id="j1", request_id="r1", event_id=None, conversation_id=None,
              team_id="dev", role="developer", stage=0, kind="pipeline",
              status="done", attempts=0, max_attempts=3, claim_token=None,
              exec_snapshot={}, payload={})
    acts = [ActionIntent(type="merge_pr", risk="reversible_internal",
                         idempotency_key="j1:0", payload={"pr": "1"})]
    out = _harden_actions(cfg_project, job, acts)
    assert len(out) == 1
    assert out[0].risk == "irreversible_outward"  # server override, not the model's


def test_harden_actions_allows_personal_converse_actions(cfg_project):
    from argus.v2.worker.worker import _harden_actions
    from argus.v2.queue.models import ActionIntent, Job
    job = Job(id="j1", request_id=None, event_id=None, conversation_id=None,
              team_id="personal", role="manager", stage=0, kind="converse",
              status="done", attempts=0, max_attempts=3, claim_token=None,
              exec_snapshot={}, payload={})
    acts = [
        ActionIntent(type="email_search", risk="irreversible_outward",
                     idempotency_key="j1:search",
                     payload={"query": "from:eesha@example.com"}),
        ActionIntent(type="calendar_create", risk="irreversible_outward",
                     idempotency_key="j1:0", payload={"title": "Call"}),
        ActionIntent(type="merge_pr", risk="reversible_internal",
                     idempotency_key="j1:1", payload={"pr": "1"}),
    ]
    out = _harden_actions(cfg_project, job, acts)
    assert [a.type for a in out] == ["email_search", "calendar_create"]
    assert [a.risk for a in out] == ["reversible_internal", "personal_outward"]


def test_harden_actions_allows_readonly_email_for_team_with_source(cfg_project):
    from argus.v2.config.schema import SourceRef
    from argus.v2.worker.worker import _harden_actions
    from argus.v2.queue.models import ActionIntent, Job

    cfg_project.company.sources.append(SourceRef(
        type="support_apps_script",
        name="support-dev",
        scope="company",
        team="dev",
        secret="k",
        config={"url": "https://example.com/app"},
    ))
    job = Job(id="j1", request_id=None, event_id=None, conversation_id=None,
              team_id="dev", role="manager", stage=0, kind="converse",
              status="done", attempts=0, max_attempts=3, claim_token=None,
              exec_snapshot={}, payload={})
    acts = [
        ActionIntent(type="email_search", risk="irreversible_outward",
                     idempotency_key="j1:search", payload={"query": "from:eesha"}),
        ActionIntent(type="email_reply", risk="irreversible_outward",
                     idempotency_key="j1:reply", payload={"thread_id": "T1", "body": "Hi"}),
    ]

    out = _harden_actions(cfg_project, job, acts)

    assert [a.type for a in out] == ["email_search"]
    assert out[0].risk == "reversible_internal"


def test_harden_actions_drops_converse_pr_op_with_bad_number(cfg_project, monkeypatch):
    """A converse PR op with a non-numeric / non-positive number is dropped."""
    from argus.v2.worker import worker as _worker
    from argus.v2.front import front as _front
    from argus.v2.queue.models import ActionIntent, Job
    monkeypatch.setattr(_front, "_gh_owner_repo", lambda cfg, t: "o/r")
    job = Job(id="j2", request_id=None, event_id=None, conversation_id=None,
              team_id="dev", role="manager", stage=0, kind="converse",
              status="done", attempts=0, max_attempts=3, claim_token=None,
              exec_snapshot={}, payload={})
    acts = [
        ActionIntent(type="close_pr", risk="x", idempotency_key="j2:0",
                     payload={"number": "not-a-number", "repo": "evil/repo"}),
        ActionIntent(type="close_pr", risk="x", idempotency_key="j2:1",
                     payload={"number": -5, "repo": "evil/repo"}),
        ActionIntent(type="close_pr", risk="x", idempotency_key="j2:2",
                     payload={"number": 9, "repo": "evil/repo"}),
    ]
    out = _worker._harden_actions(cfg_project, job, acts)
    # Only the valid one survives, with the server repo (evil/repo overridden).
    assert len(out) == 1
    assert out[0].payload["number"] == 9
    assert out[0].payload["repo"] == "o/r"


def test_config_rejects_irreversible_auto(tmp_path):
    """Loader hard-blocks autonomy.irreversible_outward=auto (defeats approval)."""
    from argus.v2.config import loader
    from argus.v2.config.loader import ConfigError
    y = tmp_path / "bad.yaml"
    y.write_text(
        "company:\n  name: c\n"
        "  defaults:\n    engine: { engine: echo }\n"
        "    autonomy: { reversible_internal: auto, irreversible_outward: auto }\n"
        "teams:\n  - name: dev\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer], max_iters: 1 }\n"
    )
    with pytest.raises(ConfigError):
        loader.load(y)


def test_parse_actions_tolerates_malformed_items():
    """A JSON array whose items are not action dicts must not raise (that would
    wedge the job into retry); bad items are skipped, a non-list yields none."""
    from argus.v2.worker.exec import _parse_actions
    from argus.v2.queue.models import Job
    job = Job(id="j3", request_id=None, event_id=None, conversation_id=None,
              team_id="dev", role="manager", stage=0, kind="converse",
              status="done", attempts=0, max_attempts=3, claim_token=None,
              exec_snapshot={}, payload={})
    text = ('ARGUS_ACTIONS: [1, "x", {"no_type": 1}, '
            '{"type": "close_pr", "payload": {"number": 5}}]')
    out = _parse_actions(job, text)
    assert len(out) == 1 and out[0].type == "close_pr"
    assert _parse_actions(job, 'ARGUS_ACTIONS: {"type": "x"}') == []


def test_converse_drops_non_allowlisted_type(conn, cfg_converse, monkeypatch):
    """A model-emitted type not in the allowlist (e.g. 'rm') is silently dropped.
    No action row, reply still works."""
    scripted = ('ARGUS_RESULT: {"action": "answer", "reply": "ok"}\n'
                'ARGUS_ACTIONS: [{"type": "rm", "risk": "reversible_internal", '
                '"payload": {}}]')
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": scripted})
    _ingest(conn, cfg_converse, key="drp1"); conn.commit()
    for _ in range(4):
        reconcile.route_events(conn, cfg_converse); conn.commit()
        while worker.run_once(cfg_converse, "w1"):
            pass
        reconcile.sweep_once(conn, cfg_converse); conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM actions WHERE type='rm'")
        assert cur.fetchone()[0] == 0, "non-allowlisted type was persisted"
        cur.execute("SELECT count(*) FROM actions WHERE type='reply' "
                    "AND idempotency_key LIKE 'converse:%%'")
        assert cur.fetchone()[0] == 1


def test_converse_personal_drops_social_publish_but_keeps_content_queue(cfg_converse):
    """Live publish needs readiness proof outside manager chat dispatch."""
    from argus.v2.queue.models import ActionIntent, Job

    job = Job(id="j-social", request_id=None, event_id=None, conversation_id=None,
              team_id="personal", role="manager", stage=0, kind="converse",
              status="done", attempts=0, max_attempts=3, claim_token=None,
              exec_snapshot={}, payload={})
    actions = [
        ActionIntent(type="social_publish", risk="personal_outward",
                     idempotency_key="publish", payload={"draft_id": "d1"}),
        ActionIntent(type="content_queue", risk="personal_outward",
                     idempotency_key="queue", payload={"project": "p"}),
    ]

    hardened = worker._harden_actions(cfg_converse, job, actions)

    assert [action.type for action in hardened] == ["content_queue"]


# ---------------------------------------------------------------------------
# project.github_repo override is typed and used by the gh-repo derivation.
# ---------------------------------------------------------------------------

def test_gh_owner_repo_uses_github_repo_override(tmp_path):
    from argus.v2.front import front
    y = tmp_path / "g.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n  - name: dev\n"
        "    project: { repo: /tmp/x, base_branch: main, test_cmd: 'true', "
        "github_repo: 'acme/widgets' }\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer], max_iters: 1 }\n"
    )
    cfg = loader.load(y)
    assert cfg.team("dev").project.github_repo == "acme/widgets"
    assert front._gh_owner_repo(cfg, "dev") == "acme/widgets"


def test_manager_state_injects_prefetched_open_prs(conn, cfg_project, monkeypatch):
    """manager_state pre-fetches open PRs (gh runs in Python, not the sandboxed
    manager) and injects them into the state block."""
    from argus.v2.front import front
    monkeypatch.setattr(front, "_gh_owner_repo", lambda cfg, t: "dangogit/sample-app")
    monkeypatch.setattr(front, "_open_prs", lambda repo: [
        {"number": 74, "title": "PostHog hardening", "isDraft": False},
        {"number": 72, "title": "Fly recovery", "isDraft": True},
    ])
    state = front.manager_state(conn, cfg_project, "dev")
    assert "Open pull requests:" in state
    assert "#74 PostHog hardening" in state
    assert "#72 Fly recovery [draft]" in state
