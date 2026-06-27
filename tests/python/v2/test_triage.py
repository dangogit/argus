"""PR-A: monitoring signals route through the team PM (manager) to triage:
investigate (researcher) / dispatch (dev pipeline) / ignore, instead of blindly
opening a code-fix pipeline. Scripted engines, no network."""
import json

import pytest

from argus.v2.config import loader
from argus.v2.ingress import events
from argus.v2.orchestrator import pipeline, reconcile
from argus.v2.worker import worker


@pytest.fixture()
def cfg_triage(tmp_path):
    y = tmp_path / "t.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n  - name: dev\n"
        "    roles:\n"
        "      - { name: manager, kind: front, prompt: m, engine: { engine: scripted } }\n"
        "      - { name: researcher, kind: worker, prompt: r, engine: { engine: scripted } }\n"
        "      - { name: developer, kind: builder, prompt: p, engine: { engine: scripted } }\n"
        "      - { name: qa, kind: judge, prompt: p }\n"
        "      - { name: senior, kind: judge, prompt: p }\n"
        "    pipeline: { stages: [developer, qa, senior], max_iters: 2 }\n")
    return loader.load(y)


def _signal(conn, cfg, fp="SIG-1", text="NullPointer in checkout, 50 events"):
    return events.ingest_signal(conn, cfg, team="dev", source="sentry-x",
                                fingerprint=fp, payload={"message": text})


def _triage(action, task="fix the checkout crash"):
    return f'ARGUS_RESULT: {json.dumps({"action": action, "task": task})}'


def _research(recommend):
    return f'ARGUS_RESULT: {json.dumps({"recommend": recommend, "summary": "root cause: null guard", "implicated_files": ["checkout.py"], "confidence": "high"})}'


def _script(monkeypatch, *, manager="", researcher="", developer="ARGUS_RESULT: {\"ready\": false, \"analysis\": \"n/a\"}"):
    out = {"manager": manager, "researcher": researcher, "developer": developer}
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: ({"scripted_output": out[r]} if out.get(r) else {}))


def _drain(conn, cfg, rounds=8):
    for _ in range(rounds):
        reconcile.sweep_once(conn, cfg); conn.commit()
        while worker.run_once(cfg, "w1"):
            conn.commit()


def _count(conn, sql, *a):
    with conn.cursor() as cur:
        cur.execute(sql, a); return cur.fetchone()[0]


def test_signal_routes_to_triage_not_blind_pipeline(conn, cfg_triage, monkeypatch):
    _script(monkeypatch, manager=_triage("ignore"))
    _signal(conn, cfg_triage); conn.commit()
    reconcile.route_events(conn, cfg_triage); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT kind, role FROM jobs WHERE team_id='dev'")
        row = cur.fetchone()
    assert row == ("triage", "manager")                     # PM triage, not a pipeline job
    assert _count(conn, "SELECT count(*) FROM requests") == 0  # nothing dispatched yet


def test_triage_ignore_opens_no_request(conn, cfg_triage, monkeypatch):
    _script(monkeypatch, manager=_triage("ignore"))
    _signal(conn, cfg_triage); conn.commit()
    _drain(conn, cfg_triage)
    assert _count(conn, "SELECT count(*) FROM requests") == 0
    assert _count(conn, "SELECT count(*) FROM jobs WHERE kind='research'") == 0


def test_triage_dispatch_opens_request(conn, cfg_triage, monkeypatch):
    _script(monkeypatch, manager=_triage("dispatch"))
    _signal(conn, cfg_triage); conn.commit()
    _drain(conn, cfg_triage)
    assert _count(conn, "SELECT count(*) FROM requests") == 1      # dev pipeline opened
    assert _count(conn, "SELECT count(*) FROM jobs WHERE kind='research'") == 0


def test_triage_investigate_runs_research_then_fix_opens_request(conn, cfg_triage, monkeypatch):
    _script(monkeypatch, manager=_triage("investigate"), researcher=_research("fix"))
    _signal(conn, cfg_triage); conn.commit()
    _drain(conn, cfg_triage)
    assert _count(conn, "SELECT count(*) FROM jobs WHERE kind='research'") == 1  # researcher ran
    assert _count(conn, "SELECT count(*) FROM requests") == 1                    # fix -> request
    # developer reuses the researcher brief (event text seeded with it)
    with conn.cursor() as cur:
        cur.execute("SELECT payload->>'text' FROM events WHERE kind='signal'")
        assert "Research brief" in cur.fetchone()[0]


def test_triage_investigate_no_fix_opens_no_request(conn, cfg_triage, monkeypatch):
    _script(monkeypatch, manager=_triage("investigate"), researcher=_research("no_fix"))
    _signal(conn, cfg_triage); conn.commit()
    _drain(conn, cfg_triage)
    assert _count(conn, "SELECT count(*) FROM jobs WHERE kind='research'") == 1
    assert _count(conn, "SELECT count(*) FROM requests") == 0    # researcher said no fix


def test_recurring_signal_not_re_triaged_while_in_flight(conn, cfg_triage, monkeypatch):
    _script(monkeypatch, manager=_triage("ignore"))
    _signal(conn, cfg_triage, fp="SIG-DUP"); conn.commit()
    reconcile.route_events(conn, cfg_triage); conn.commit()
    # same fingerprint re-emitted (event re-marked received): no second triage job
    with conn.cursor() as cur:
        cur.execute("UPDATE events SET status='received', processed_at=NULL "
                    "WHERE source='sentry-x'")
    conn.commit()
    reconcile.route_events(conn, cfg_triage); conn.commit()
    assert _count(conn, "SELECT count(*) FROM jobs WHERE kind='triage'") == 1


def test_non_conversational_team_keeps_direct_pipeline(conn, tmp_path):
    # back-compat: a team with no manager routes signals straight to the pipeline
    y = tmp_path / "n.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n  - name: dev\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n")
    cfg = loader.load(y)
    events.ingest_signal(conn, cfg, team="dev", source="s", fingerprint="S2",
                         payload={"message": "boom"}); conn.commit()
    reconcile.route_events(conn, cfg); conn.commit()
    assert _count(conn, "SELECT count(*) FROM requests") == 1
    assert _count(conn, "SELECT count(*) FROM jobs WHERE kind='triage'") == 0


def test_branch_drift_signal_notifies_owner_not_pipeline(conn, tmp_path):
    # drift -> notify the control channel; no triage job, no request, no dispatch
    y = tmp_path / "d.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n  - name: dev\n"
        "    roles: [ { name: manager, kind: front, prompt: m, engine: { engine: scripted } },"
        " { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "    channels: [ { type: whatsapp, role: control, channel_id: 'grp1' } ]\n")
    from argus.v2.config import loader as _l
    cfg = _l.load(y)
    events.ingest_signal(conn, cfg, team="dev", source="drift-dev", fingerprint="DR-1",
                         payload={"kind": "branch_drift", "message": "staging is 3 behind main"})
    conn.commit()
    reconcile.route_events(conn, cfg); conn.commit()
    assert _count(conn, "SELECT count(*) FROM jobs WHERE kind='triage'") == 0
    assert _count(conn, "SELECT count(*) FROM requests") == 0
    with conn.cursor() as cur:
        cur.execute("SELECT destination_ref, payload->>'text' FROM actions WHERE type='notify'")
        dest, text = cur.fetchone()
        cur.execute("SELECT context_type, context_ref, summary "
                    "FROM conversation_contexts WHERE team_id='dev'")
        context_type, context_ref, summary = cur.fetchone()
    assert dest == "whatsapp:grp1" and "3 behind" in text
    assert context_type == "branch_drift"
    assert context_ref == "DR-1"
    assert "3 behind" in summary


def test_branch_drift_followup_do_it_opens_sync_request(conn, tmp_path):
    y = tmp_path / "d.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n  - name: dev\n"
        "    project: { repo: /tmp/x, base_branch: staging, test_cmd: 'true' }\n"
        "    roles: [ { name: manager, kind: front, prompt: m, engine: { engine: scripted } },"
        " { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "    channels: [ { type: whatsapp, role: control, channel_id: 'grp1' } ]\n")
    from argus.v2.config import loader as _l
    cfg = _l.load(y)
    events.ingest_signal(conn, cfg, team="dev", source="drift-dev", fingerprint="DR-1",
                         payload={
                             "kind": "branch_drift",
                             "message": "branch staging is 3 behind main (dev)",
                             "base": "main",
                             "head": "staging",
                             "ahead": 0,
                             "behind": 3,
                         })
    conn.commit()
    reconcile.route_events(conn, cfg)
    conn.commit()

    events.ingest_message(conn, cfg, team="dev", source="whatsapp:grp1",
                          dedup_key="m1", text="do it",
                          conversation_key="whatsapp:grp1")
    conn.commit()
    reconcile.route_events(conn, cfg)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs WHERE kind='converse'")
        converse_jobs = cur.fetchone()[0]
        cur.execute("SELECT count(*), max(fingerprint) FROM requests")
        request_count, fingerprint = cur.fetchone()
        cur.execute("SELECT payload->>'text' FROM events "
                    "WHERE kind='message' AND dedup_key='m1'")
        task = cur.fetchone()[0]
        cur.execute("SELECT payload->>'text' FROM actions WHERE type='reply' "
                    "ORDER BY created_at DESC LIMIT 1")
        reply = cur.fetchone()[0]
        cur.execute("SELECT status FROM conversation_contexts WHERE context_ref='DR-1'")
        status = cur.fetchone()[0]

    assert converse_jobs == 0
    assert request_count == 1
    assert fingerprint == "DR-1"
    assert "Owner approved branch drift sync" in task
    assert "branch staging is 3 behind main" in task
    assert "On it" in reply
    assert status == "resolved"
