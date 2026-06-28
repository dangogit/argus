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


def test_nonce_is_128_bit(conn, cfg):
    rid, aid, nonce = _pending(conn, cfg)
    assert len(nonce) == 32  # 16 bytes hex = 128 bits
    int(nonce, 16)  # valid hex, no exception


def _new_proposed_action(conn, rid, key):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO actions (request_id, team_id, type, risk, idempotency_key) "
            "VALUES (%s,'dev','merge','irreversible_outward',%s) RETURNING id", (rid, key))
        return str(cur.fetchone()[0])


def test_nonce_collision_retries_no_stuck_action(conn, cfg, monkeypatch):
    # Occupy a nonce, then force the first generated token to collide with it.
    # The action must still get a real (different) nonce and be parked - never
    # left awaiting_approval with no approval row (the old DO NOTHING bug).
    rid, _aid, existing = _pending(conn, cfg); conn.commit()
    aid2 = _new_proposed_action(conn, rid, "a1"); conn.commit()

    fresh = "a" * 32
    real = executor.secrets.token_hex
    calls = {"n": 0}

    def fake_token_hex(n):
        calls["n"] += 1
        if calls["n"] == 1:
            return existing   # collide on the first try
        if calls["n"] == 2:
            return fresh      # succeed on the retry
        return real(n)

    monkeypatch.setattr(executor.secrets, "token_hex", fake_token_hex)
    executor.process_proposed(conn, cfg); conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT nonce FROM approvals WHERE action_id=%s", (aid2,))
        assert cur.fetchone()[0] == fresh  # retried past the collision
        cur.execute("SELECT status FROM actions WHERE id=%s", (aid2,))
        assert cur.fetchone()[0] == "awaiting_approval"  # parked with a nonce, not stuck


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
