from datetime import datetime, timedelta, timezone

from argus.v2.actions import approvals, executor
from argus.v2.ingress import events
from argus.v2.orchestrator import pipeline


def _pending(conn, cfg):
    eid = events.ingest_message(conn, cfg, team="dev", source="cli",
                                dedup_key="m1", text="t")
    rid = pipeline.open_request(conn, cfg, event_id=eid, team_id="dev",
                                conversation_id=None)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO actions (request_id, team_id, type, risk, idempotency_key) "
            "VALUES (%s,'dev','merge','irreversible_outward','a0') RETURNING id", (rid,))
        aid = str(cur.fetchone()[0])
    executor.process_proposed(conn, cfg)
    with conn.cursor() as cur:
        cur.execute("SELECT nonce FROM approvals WHERE action_id=%s", (aid,))
        nonce = cur.fetchone()[0]
    return rid, aid, nonce


def test_approve_consumes_and_executes(conn, cfg):
    rid, aid, nonce = _pending(conn, cfg); conn.commit()
    assert approvals.consume(conn, nonce, decision="approved", approver_ref="cli") is True
    conn.commit()
    executor.process_proposed(conn, cfg); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM actions WHERE id=%s", (aid,))
        assert cur.fetchone()[0] == "done"
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] == "open"  # resumed


def test_second_approve_is_noop(conn, cfg):
    rid, aid, nonce = _pending(conn, cfg); conn.commit()
    assert approvals.consume(conn, nonce, decision="approved", approver_ref="cli") is True
    conn.commit()
    assert approvals.consume(conn, nonce, decision="approved", approver_ref="cli") is False
    conn.commit()


def test_reject_cancels_request(conn, cfg):
    rid, aid, nonce = _pending(conn, cfg); conn.commit()
    approvals.consume(conn, nonce, decision="rejected", approver_ref="cli"); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM actions WHERE id=%s", (aid,))
        assert cur.fetchone()[0] == "rejected"
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] == "cancelled"


def test_expire_due_marks_expired(conn, cfg):
    rid, aid, nonce = _pending(conn, cfg)
    with conn.cursor() as cur:
        cur.execute("UPDATE approvals SET expires_at=now()-interval '1 min' WHERE nonce=%s", (nonce,))
    conn.commit()
    assert approvals.expire_due(conn) == 1
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM approvals WHERE nonce=%s", (nonce,))
        assert cur.fetchone()[0] == "expired"
