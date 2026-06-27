from datetime import datetime, timezone

from argus.v2.context import assemble as ctx
from argus.v2.ingress import events


def test_assemble_includes_last_24h_messages(conn, cfg):
    events.ingest_message(conn, cfg, team="dev", source="cli", dedup_key="m1", text="hello team")
    conn.commit()
    eid = events.ingest_message(conn, cfg, team="dev", source="cli", dedup_key="m2", text="fix login")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT conversation_id FROM events WHERE dedup_key='m2'")
        conv = cur.fetchone()[0]
    bundle = ctx.assemble(conn, team_id="dev", conversation_id=None,
                          now=datetime.now(timezone.utc))
    texts = [t for _, t in bundle.recent_messages]
    assert "hello team" in texts and "fix login" in texts
    assert "fix login" in bundle.as_prompt()


def test_assemble_omits_old_messages(conn, cfg):
    events.ingest_message(conn, cfg, team="dev", source="cli", dedup_key="old", text="ancient")
    with conn.cursor() as cur:
        cur.execute("UPDATE events SET received_at = now() - interval '48 hours' WHERE dedup_key='old'")
    conn.commit()
    bundle = ctx.assemble(conn, team_id="dev", conversation_id=None,
                          now=datetime.now(timezone.utc))
    assert all("ancient" != t for _, t in bundle.recent_messages)


from datetime import date

from argus.v2.context import summarize


def test_summarize_day_writes_a_row(conn, cfg):
    events.ingest_message(conn, cfg, team="dev", source="cli", dedup_key="m1", text="hi")
    events.ingest_message(conn, cfg, team="dev", source="cli", dedup_key="m2", text="fix bug")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT (now() AT TIME ZONE 'utc')::date")
        today = cur.fetchone()[0]
    s = summarize.summarize_day(conn, cfg, team_id="dev", conversation_id=None, day=today)
    conn.commit()
    assert s is not None and "2 message" in s
    with conn.cursor() as cur:
        cur.execute("SELECT message_count FROM conversation_summaries WHERE team_id='dev'")
        assert cur.fetchone()[0] == 2


def test_summarize_day_is_idempotent(conn, cfg):
    events.ingest_message(conn, cfg, team="dev", source="cli", dedup_key="m1", text="hi")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT (now() AT TIME ZONE 'utc')::date")
        today = cur.fetchone()[0]
    summarize.summarize_day(conn, cfg, team_id="dev", conversation_id=None, day=today); conn.commit()
    summarize.summarize_day(conn, cfg, team_id="dev", conversation_id=None, day=today); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM conversation_summaries WHERE team_id='dev'")
        assert cur.fetchone()[0] == 1  # upsert, not duplicate
