"""Fixes for owner-reported WhatsApp noise + junk intake (2026-06-19):

- alerts.record cooldown so a flapping check (watchdog) re-alerts at most once
  per window instead of every run.
- pipeline.is_actionable gate so internal-noise / empty signals never open a
  build request (the "triage produced no findings" -> pipeline -> owner loop).
- pipeline._fail routes signal-origin failures to the log, not the owner's
  WhatsApp (owner only hears back about requests they actually made).
- pipeline._no_fix_close does the same for signal-origin no-fix/blocked output,
  so automated scans can be silent without making the owner blind.
- context source resolves $HOME even when launchd drops HOME from the env.
"""
import pytest

from argus.v2 import alerts
from argus.v2.context import source
from argus.v2.ingress import events
from argus.v2.orchestrator import pipeline, reconcile


# --- A1: alert cooldown (watchdog flap suppression) ---

def test_record_cooldown_suppresses_repeat(conn):
    a1 = alerts.record(conn, severity="error", project="general",
                       fingerprint="argus-watchdog-readiness",
                       message="readiness failed", cooldown_seconds=3600)
    conn.commit()
    a2 = alerts.record(conn, severity="error", project="general",
                       fingerprint="argus-watchdog-readiness",
                       message="readiness failed", cooldown_seconds=3600)
    conn.commit()
    assert a1 is not None
    assert a2 is None  # within cooldown -> suppressed
    rows = [r for r in alerts.list_alerts(conn, project="general")
            if r.fingerprint == "argus-watchdog-readiness"]
    assert len(rows) == 1


def test_record_without_cooldown_still_inserts_repeat(conn):
    alerts.record(conn, severity="warn", project="general",
                  fingerprint="fp", message="m"); conn.commit()
    alerts.record(conn, severity="warn", project="general",
                  fingerprint="fp", message="m"); conn.commit()
    rows = [r for r in alerts.list_alerts(conn, project="general")
            if r.fingerprint == "fp"]
    assert len(rows) == 2


def test_owner_alert_same_fingerprint_updated_at_dedups_without_cooldown(conn):
    evidence_ids = [
        "5a955738-9c67-4007-ad17-9dba0ad260c0",
        "240f8e8d-2cbf-435c-9a8d-095771dfc5c4",
        "5330ed6d-6785-4b40-892f-0801a77b994b",
        "b667c413-abf8-4406-9845-1e9d96be09ff",
        "3cc962f4-0ffb-4993-92b6-ea540af57f17",
    ]
    for evidence_id in evidence_ids:
        alerts.record(conn, severity="error", project="general",
                      fingerprint="disk:low:argus-run",
                      message="low disk space under /Users/danielmini/argus-run",
                      channel="whatsapp",
                      payload={
                          "updated_at": "2026-07-03T08:30:00+03:00",
                          "evidence_id": evidence_id,
                      })
        conn.commit()

    rows = [r for r in alerts.list_alerts(conn, project="general")
            if r.fingerprint == "disk:low:argus-run"]
    assert len(rows) == 1


# --- A3/B1: non-actionable signals don't open requests ---

@pytest.mark.parametrize("payload", [
    {"text": ""},
    {"text": "   "},
    {},
    {"text": "triage produced no findings (all sources skipped or empty)"},
    {"text": "produced no findings"},
])
def test_is_actionable_rejects_noise(payload):
    assert pipeline.is_actionable(payload) is False


@pytest.mark.parametrize("payload", [
    {"text": "Sentry: NullPointer in checkout"},
    {"text": "uptime: api.example.com is down"},
    {"title": "NullPointer in checkout", "level": "error"},  # sentry shape, no "text"
])
def test_is_actionable_accepts_real_signal(payload):
    assert pipeline.is_actionable(payload) is True


def test_route_events_drops_noise_signal(conn, cfg_project):
    events.ingest_signal(conn, cfg_project, team="dev", source="triage",
                         fingerprint="noise1",
                         payload={"text": "triage produced no findings"})
    conn.commit()
    reconcile.route_events(conn, cfg_project); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM requests")
        assert cur.fetchone()[0] == 0
        # event still consumed, not left to re-claim forever
        cur.execute("SELECT status FROM events WHERE dedup_key='noise1'")
        assert cur.fetchone()[0] == "processed"


def test_route_events_opens_real_signal(conn, cfg_project):
    events.ingest_signal(conn, cfg_project, team="dev", source="sentry",
                         fingerprint="real1",
                         payload={"text": "Sentry: NullPointer in checkout"})
    conn.commit()
    reconcile.route_events(conn, cfg_project); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM requests")
        assert cur.fetchone()[0] == 1


# --- A2: signal-origin failures log instead of pinging the owner ---

def test_fail_signal_origin_logs_not_notifies(conn, cfg_project):
    eid = events.ingest_signal(conn, cfg_project, team="dev", source="sentry",
                               fingerprint="f-a2", payload={"text": "Sentry: boom"})
    rid = pipeline.open_request(conn, cfg_project, event_id=eid, team_id="dev",
                                conversation_id=None)
    conn.commit()
    pipeline._fail(conn, cfg_project, rid, "qa did not pass")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM actions WHERE request_id=%s AND type='notify'",
                    (rid,))
        assert cur.fetchone()[0] == 0  # no owner ping for an auto/signal request
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] == "failed"
    logged = [r for r in alerts.list_alerts(conn, project="dev")
              if r.channel == "log" and "pipeline" in r.message.lower()]
    assert logged, "signal-origin failure should be logged"


def test_fail_owner_origin_still_notifies(conn, cfg_project):
    eid = events.ingest_message(conn, cfg_project, team="dev", source="wa",
                                dedup_key="m-a2", text="fix the thing",
                                conversation_key="wa:group1")
    with conn.cursor() as cur:
        cur.execute("SELECT conversation_id FROM events WHERE id=%s", (eid,))
        conv_id = str(cur.fetchone()[0])
    rid = pipeline.open_request(conn, cfg_project, event_id=eid, team_id="dev",
                                conversation_id=conv_id)
    conn.commit()
    pipeline._fail(conn, cfg_project, rid, "qa did not pass")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM actions WHERE request_id=%s AND type='notify'",
                    (rid,))
        assert cur.fetchone()[0] == 1  # owner asked -> owner hears back


def test_no_fix_signal_origin_logs_not_notifies(conn, cfg_project):
    eid = events.ingest_signal(conn, cfg_project, team="dev", source="posthog",
                               fingerprint="nf-a2", payload={"text": "TypeError"})
    rid = pipeline.open_request(conn, cfg_project, event_id=eid, team_id="dev",
                                conversation_id=None)
    conn.commit()
    pipeline._no_fix_close(conn, cfg_project, rid, "No code fix warranted.")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM actions WHERE request_id=%s AND type='notify'",
                    (rid,))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] == "done"
    logged = [r for r in alerts.list_alerts(conn, project="dev")
              if r.channel == "log" and r.severity == "info"
              and "No code fix warranted" in r.message]
    assert logged, "signal-origin no-fix should be logged"


# --- B1/B2: dispatch task cleanup (collapse doubled text, drop degenerate) ---

@pytest.mark.parametrize("raw,expected", [
    ("לא עובד לא עובד", "לא עובד"),
    ("Fix login. Fix login.", "Fix login."),
    ("fix login fix login", "fix login"),
    ("fix login bug now", "fix login bug now"),  # not a whole-string repeat
    ("deploy deploy deploy", "deploy"),
    ("no repeat here", "no repeat here"),
    ("  spaced   out  ", "spaced out"),
])
def test_collapse_repeat(raw, expected):
    assert pipeline.collapse_repeat(raw) == expected


@pytest.mark.parametrize("text", ["", "   ", "fix", "x x", "go", "deploy deploy"])
def test_too_vague_true(text):
    assert pipeline.too_vague_to_dispatch(text) is True


@pytest.mark.parametrize("text", [
    "Fix the login redirect",
    "checkout returns 500 on submit",
    "fix login bug",
])
def test_too_vague_false(text):
    assert pipeline.too_vague_to_dispatch(text) is False


# --- D1: $HOME resolves even when launchd drops HOME from the env ---

def test_context_db_resolves_home_without_env(monkeypatch):
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("ARGUS_CONTEXT_DB", "$HOME/nonexistent-xyz.db")
    monkeypatch.setenv("ARGUS_CONTEXT_TABLE", "messages")
    with pytest.raises(source.SourceError) as ei:
        source._fetch_sqlite(0, 10)
    # error proves $HOME was expanded to a real path, not left literal
    assert "$HOME" not in str(ei.value)
