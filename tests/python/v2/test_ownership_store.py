from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import psycopg
import pytest

from argus.v2.ownership import store


def _obligation(conn, *, kind="code", fingerprint="sentry:abc"):
    return store.upsert(
        conn,
        team_id="dev",
        kind=kind,
        fingerprint=fingerprint,
        title="Fix crash",
        source_ref=fingerprint,
        definition_of_done={"pr": True},
    )


def _events(conn, obligation_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT from_status, to_status, reason, evidence
            FROM team_obligation_events
            WHERE obligation_id=%s
            ORDER BY id
            """,
            (obligation_id,),
        )
        return [
            {
                "from_status": row[0],
                "to_status": row[1],
                "reason": row[2],
                "evidence": row[3],
            }
            for row in cur.fetchall()
        ]


def test_upsert_is_idempotent(conn):
    first = _obligation(conn)
    second = store.upsert(
        conn,
        team_id="dev",
        kind="code",
        fingerprint="sentry:abc",
        title="Fix crash again",
        source_ref="sentry:abc",
        definition_of_done={"pr": True},
    )

    assert second.id == first.id


def test_concurrent_upsert_returns_one_obligation(pg_dsn, conn):
    barrier = Barrier(2)

    def create():
        with psycopg.connect(pg_dsn) as worker_conn:
            barrier.wait()
            return store.upsert(
                worker_conn,
                team_id="dev",
                kind="code",
                fingerprint="sentry:race",
                title="Fix race",
                source_ref="sentry:race",
                definition_of_done={"pr": True},
            ).id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: create(), range(2)))

    assert ids[0] == ids[1]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM team_obligations WHERE fingerprint='sentry:race'"
        )
        assert cur.fetchone()[0] == 1


def test_transition_records_event_and_terminal_timestamp(conn):
    item = _obligation(conn)
    store.transition(conn, item.id, to_status="working", reason="request queued")
    store.transition(conn, item.id, to_status="verifying", reason="preview ready")
    done = store.transition(
        conn,
        item.id,
        to_status="done",
        reason="smoke passed",
        evidence={"status_code": 200},
    )

    assert done.completed_at is not None
    assert _events(conn, item.id)[-1]["evidence"]["status_code"] == 200


def test_transition_rejects_autocommit_without_mutation(pg_dsn, conn):
    item = _obligation(conn)
    conn.commit()

    with psycopg.connect(pg_dsn, autocommit=True) as autocommit_conn:
        with pytest.raises(ValueError, match="autocommit"):
            store.transition(
                autocommit_conn,
                item.id,
                to_status="working",
                reason="unsafe transaction",
                evidence={"request_id": "req-1"},
            )

    assert store.get(conn, item.id).status == "open"
    assert store.get(conn, item.id).evidence == {}
    assert _events(conn, item.id) == []


def test_illegal_transition_fails_closed(conn):
    item = _obligation(conn)

    with pytest.raises(ValueError, match="open -> done"):
        store.transition(conn, item.id, to_status="done", reason="shortcut")

    assert store.get(conn, item.id).status == "open"
    assert _events(conn, item.id) == []


@pytest.mark.parametrize("kind", ["code", "support", "maintenance"])
def test_obligation_cannot_complete_from_working(conn, kind):
    item = _obligation(conn, kind=kind, fingerprint=f"{kind}:working")
    store.transition(conn, item.id, to_status="working", reason="work started")

    with pytest.raises(ValueError, match="working -> done"):
        store.transition(
            conn,
            item.id,
            to_status="done",
            reason="unverified completion",
            evidence={"claimed_done": True},
        )

    assert store.get(conn, item.id).status == "working"
    assert store.get(conn, item.id).evidence == {}
    assert len(_events(conn, item.id)) == 1


def test_code_cannot_complete_from_awaiting_approval(conn):
    item = _obligation(conn)
    store.transition(
        conn,
        item.id,
        to_status="awaiting_approval",
        reason="owner decision needed",
    )

    with pytest.raises(ValueError, match="awaiting_approval -> done"):
        store.transition(
            conn,
            item.id,
            to_status="done",
            reason="approval assumed",
            evidence={"approved": False},
        )

    assert store.get(conn, item.id).status == "awaiting_approval"
    assert store.get(conn, item.id).evidence == {}
    assert len(_events(conn, item.id)) == 1


def test_code_completes_from_verifying_with_evidence(conn):
    item = _obligation(conn)
    store.transition(conn, item.id, to_status="working", reason="work started")
    store.transition(conn, item.id, to_status="verifying", reason="preview ready")

    done = store.transition(
        conn,
        item.id,
        to_status="done",
        reason="verification passed",
        evidence={"status_code": 200},
    )

    assert done.status == "done"
    assert done.evidence == {"status_code": 200}
    assert done.completed_at is not None


def test_same_status_transition_is_idempotent_under_concurrency(pg_dsn, conn):
    item = _obligation(conn)
    conn.commit()
    barrier = Barrier(2)

    def start_work():
        with psycopg.connect(pg_dsn) as worker_conn:
            barrier.wait()
            return store.transition(
                worker_conn,
                item.id,
                to_status="working",
                reason="request queued",
            ).status

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: start_work(), range(2)))

    assert statuses == ["working", "working"]
    assert len(_events(conn, item.id)) == 1


def test_same_status_transition_records_new_reason_and_evidence(conn):
    item = _obligation(conn)
    store.transition(
        conn,
        item.id,
        to_status="working",
        reason="request queued",
        evidence={"request_id": "req-1"},
    )

    updated = store.transition(
        conn,
        item.id,
        to_status="working",
        reason="worker claimed",
        evidence={"worker_id": "worker-1"},
    )
    repeated = store.transition(
        conn,
        item.id,
        to_status="working",
        reason="worker claimed",
        evidence={"worker_id": "worker-1"},
    )

    events = _events(conn, item.id)
    assert updated.evidence == {"request_id": "req-1", "worker_id": "worker-1"}
    assert repeated == updated
    assert len(events) == 2
    assert events[-1] == {
        "from_status": "working",
        "to_status": "working",
        "reason": "worker claimed",
        "evidence": {"worker_id": "worker-1"},
    }


def test_concurrent_same_status_updates_preserve_differing_events(pg_dsn, conn):
    item = _obligation(conn)
    store.transition(
        conn,
        item.id,
        to_status="working",
        reason="request queued",
        evidence={"request_id": "req-1"},
    )
    conn.commit()
    barrier = Barrier(2)

    def record(update):
        reason, evidence = update
        with psycopg.connect(pg_dsn) as worker_conn:
            barrier.wait()
            return store.transition(
                worker_conn,
                item.id,
                to_status="working",
                reason=reason,
                evidence=evidence,
            ).status

    updates = [
        ("worker heartbeat", {"heartbeat": "one"}),
        ("review attached", {"review": "ready"}),
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(record, updates))

    events = _events(conn, item.id)
    current = store.get(conn, item.id)
    assert statuses == ["working", "working"]
    assert len(events) == 3
    assert {event["reason"] for event in events[1:]} == {
        "worker heartbeat",
        "review attached",
    }
    assert current.evidence == {
        "request_id": "req-1",
        "heartbeat": "one",
        "review": "ready",
    }


def test_get_returns_none_for_missing_obligation(conn):
    assert store.get(conn, "00000000-0000-0000-0000-000000000000") is None


def test_list_due_filters_terminal_and_future_rows_and_orders_priority(conn):
    low = _obligation(conn)
    high = store.upsert(
        conn,
        team_id="dev",
        kind="maintenance",
        fingerprint="health:high",
        title="Repair worker",
        source_ref="health:high",
        definition_of_done={"healthy": True},
    )
    future = store.upsert(
        conn,
        team_id="dev",
        kind="support",
        fingerprint="support:future",
        title="Reply later",
        source_ref="support:future",
        definition_of_done={"reply": True},
    )
    terminal = store.upsert(
        conn,
        team_id="dev",
        kind="code",
        fingerprint="sentry:failed",
        title="Cannot reproduce",
        source_ref="sentry:failed",
        definition_of_done={"pr": True},
    )
    other_team = store.upsert(
        conn,
        team_id="ops",
        kind="maintenance",
        fingerprint="health:ops",
        title="Repair ops worker",
        source_ref="health:ops",
        definition_of_done={"healthy": True},
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE team_obligations
            SET next_check_at=clock_timestamp() - interval '1 minute',
                priority=CASE WHEN id=%s THEN 90 ELSE 10 END
            WHERE id IN (%s, %s)
            """,
            (high.id, low.id, high.id),
        )
        cur.execute(
            "UPDATE team_obligations SET next_check_at=clock_timestamp() + interval '1 hour' WHERE id=%s",
            (future.id,),
        )
    store.transition(conn, terminal.id, to_status="failed", reason="not actionable")

    assert [item.id for item in store.list_due(conn, team_id="dev")] == [high.id, low.id]
    assert [item.id for item in store.list_due(conn, limit=1)] == [high.id]
    assert other_team.id in [item.id for item in store.list_due(conn)]


def test_link_request_action_and_increment_attempts(conn):
    item = _obligation(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO events (team_id, kind, source, payload, dedup_key)
            VALUES ('dev', 'signal', 'test', '{}'::jsonb, 'ownership-links')
            RETURNING id
            """
        )
        event_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO requests (event_id, team_id) VALUES (%s, 'dev') RETURNING id",
            (event_id,),
        )
        request_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO actions (request_id, team_id, type, risk, idempotency_key)
            VALUES (%s, 'dev', 'notify', 'reversible_internal', 'ownership-links')
            RETURNING id
            """,
            (request_id,),
        )
        action_id = cur.fetchone()[0]

    linked_request = store.link_request(conn, item.id, request_id)
    linked_action = store.link_action(conn, item.id, action_id)
    first_attempt = store.increment_attempts(conn, item.id)
    second_attempt = store.increment_attempts(conn, item.id)

    assert linked_request.request_id == request_id
    assert linked_action.action_id == action_id
    assert first_attempt.attempts == 1
    assert second_attempt.attempts == 2
