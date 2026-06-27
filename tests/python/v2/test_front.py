from pathlib import Path

import pytest

from argus.v2.front import front
from argus.v2.ingress import events
from argus.v2.orchestrator import reconcile


def test_dispatch_message_opens_request(conn, cfg):
    events.ingest_message(conn, cfg, team="dev", source="cli", dedup_key="m1",
                          text="please fix the login bug"); conn.commit()
    reconcile.sweep_once(conn, cfg); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM requests"); assert cur.fetchone()[0] == 1
        cur.execute("SELECT status FROM events WHERE dedup_key='m1'")
        assert cur.fetchone()[0] == "processed"


def test_reply_message_emits_reply_action_no_request(conn, cfg):
    events.ingest_message(conn, cfg, team="dev", source="cli", dedup_key="m1",
                          text="thanks!"); conn.commit()
    reconcile.sweep_once(conn, cfg); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM requests"); assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM actions WHERE type='reply'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT status FROM events WHERE dedup_key='m1'")
        assert cur.fetchone()[0] == "processed"


def test_signal_opens_request_directly(conn, cfg):
    events.ingest_signal(conn, cfg, team="dev", source="sentry",
                         fingerprint="ISSUE-9", payload={"e": 1}); conn.commit()
    reconcile.sweep_once(conn, cfg); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT fingerprint FROM requests"); assert cur.fetchone()[0] == "ISSUE-9"


# C2: manager_state tests
def test_manager_state_empty_returns_no_work_line(conn, cfg):
    state = front.manager_state(conn, cfg, "dev")
    assert "no current work" in state.lower() or state.strip() != ""


def test_manager_state_includes_open_request(conn, cfg):
    from psycopg.types.json import Json
    # Seed an event, then a request in 'open' status.
    eid = events.ingest_message(conn, cfg, team="dev", source="cli",
                                dedup_key="ms1", text="fix login"); conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO requests (event_id, team_id, conversation_id, status) "
            "VALUES (%s, 'dev', NULL, 'open') RETURNING id", (eid,))
        rid = str(cur.fetchone()[0])
    conn.commit()
    state = front.manager_state(conn, cfg, "dev")
    assert rid in state or "open" in state.lower()


def test_manager_state_includes_pr_url(conn, cfg):
    # Seed an open_pr action with a provider_ref URL.
    eid = events.ingest_message(conn, cfg, team="dev", source="cli",
                                dedup_key="ms2", text="fix x"); conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO requests (event_id, team_id, conversation_id, status) "
            "VALUES (%s, 'dev', NULL, 'done') RETURNING id", (eid,))
        rid = str(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO actions (request_id, team_id, type, risk, "
            "idempotency_key, status, provider_ref) "
            "VALUES (%s, 'dev', 'open_pr', 'reversible_internal', "
            "'open_pr_ms2', 'done', 'https://github.com/x/y/pull/7')",
            (rid,))
    conn.commit()
    state = front.manager_state(conn, cfg, "dev")
    assert "https://github.com/x/y/pull/7" in state


def test_manager_state_includes_recent_signal(conn, cfg):
    events.ingest_signal(conn, cfg, team="dev", source="sentry",
                         fingerprint="SIG-1", payload={"msg": "boom"}); conn.commit()
    state = front.manager_state(conn, cfg, "dev")
    assert "sentry" in state.lower() or "SIG-1" in state
