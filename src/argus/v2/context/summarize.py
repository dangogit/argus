"""Roll up one day of a team's conversation + activity into a durable summary.
Slice 1 is a deterministic rollup; a configured engine writes a real prose
summary once the live front lands (slice 4)."""
from __future__ import annotations

from datetime import date
from typing import Optional


def summarize_day(conn, cfg, *, team_id: str, conversation_id, day: date) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT count(*) FILTER (WHERE kind='message') FROM events
               WHERE team_id=%s AND (%s::uuid IS NULL OR conversation_id = %s::uuid)
                 AND (received_at AT TIME ZONE 'utc')::date = %s""",
            (team_id, conversation_id, conversation_id, day))
        msgs = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM requests WHERE team_id=%s "
            "AND (created_at AT TIME ZONE 'utc')::date = %s", (team_id, day))
        reqs = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM actions WHERE team_id=%s AND status='done' "
            "AND (created_at AT TIME ZONE 'utc')::date = %s", (team_id, day))
        acts = cur.fetchone()[0]
    if msgs == 0 and reqs == 0:
        return None
    summary = f"{msgs} message(s); {reqs} request(s) opened; {acts} action(s) completed."
    with conn.cursor() as cur:
        if conversation_id is None:
            # NULL is not equal to NULL in UNIQUE constraints, so handle it explicitly.
            cur.execute(
                "SELECT id FROM conversation_summaries "
                "WHERE team_id=%s AND conversation_id IS NULL AND day=%s",
                (team_id, day))
            row = cur.fetchone()
            if row:
                cur.execute(
                    "UPDATE conversation_summaries SET summary=%s, message_count=%s, created_at=now() "
                    "WHERE id=%s", (summary, msgs, row[0]))
            else:
                cur.execute(
                    "INSERT INTO conversation_summaries (team_id, conversation_id, day, summary, message_count) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (team_id, conversation_id, day, summary, msgs))
        else:
            cur.execute(
                """INSERT INTO conversation_summaries (team_id, conversation_id, day, summary, message_count)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (team_id, conversation_id, day)
                   DO UPDATE SET summary=EXCLUDED.summary, message_count=EXCLUDED.message_count,
                                 created_at=now()""",
                (team_id, conversation_id, day, summary, msgs))
    return summary
