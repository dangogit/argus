"""Approval lifecycle. Consume is a CAS on the pending row, so a repeated or
ambiguous approval cannot resume twice. Approve -> action back to 'proposed'
(so the executor runs it) + request resumes 'open'. Reject -> action rejected +
request cancelled. Expiry -> like reject, but status 'expired'."""
from __future__ import annotations

import psycopg


def consume(conn: psycopg.Connection, nonce: str, *, decision: str, approver_ref: str) -> bool:
    assert decision in ("approved", "rejected")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE approvals SET status=%s, approver_ref=%s, consumed_at=now() "
            "WHERE nonce=%s AND status='pending' RETURNING action_id, request_id",
            (decision, approver_ref, nonce))
        row = cur.fetchone()
        if not row:
            return False  # already consumed / unknown nonce
        action_id, request_id = row
        if decision == "approved":
            # Re-arm the action for the executor to run, resume the request.
            # Use 'approved' (not 'proposed') so the executor skips the policy
            # gate and executes directly without requiring a second approval.
            cur.execute("UPDATE actions SET status='approved', updated_at=now() WHERE id=%s",
                        (action_id,))
            if request_id is not None:
                cur.execute("UPDATE requests SET status='open', updated_at=now() "
                            "WHERE id=%s AND status='awaiting_approval'", (request_id,))
        else:
            cur.execute("UPDATE actions SET status='rejected', updated_at=now() WHERE id=%s",
                        (action_id,))
            if request_id is not None:
                cur.execute("UPDATE requests SET status='cancelled', updated_at=now() "
                            "WHERE id=%s AND status='awaiting_approval'", (request_id,))
    return True


def expire_due(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE approvals SET status='expired', consumed_at=now() "
            "WHERE status='pending' AND expires_at < now() "
            "RETURNING action_id, request_id")
        rows = cur.fetchall()
        for action_id, request_id in rows:
            cur.execute("UPDATE actions SET status='rejected', updated_at=now() WHERE id=%s",
                        (action_id,))
            if request_id is not None:
                cur.execute("UPDATE requests SET status='cancelled', updated_at=now() "
                            "WHERE id=%s AND status='awaiting_approval'", (request_id,))
        return len(rows)
