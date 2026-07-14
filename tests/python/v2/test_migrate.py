def test_core_tables_exist(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        tables = {r[0] for r in cur.fetchall()}
    for t in ("conversations", "conversation_contexts", "events", "media",
              "requests", "jobs", "runs", "actions", "approvals",
              "schema_migrations"):
        assert t in tables


def test_migrate_is_idempotent(conn):
    from pathlib import Path
    from argus.v2.db import migrate
    repo = Path(__file__).resolve().parents[3]
    applied = migrate.apply(conn, repo / "src/argus/v2/db/migrations")
    assert applied == []  # already applied by the session fixture


def test_conversation_summary_memory_columns_have_safe_defaults(conn):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO conversation_summaries
               (team_id, day, summary, message_count)
               VALUES ('dev', CURRENT_DATE, 'legacy summary', 1)
               RETURNING details, source_fingerprint, updated_at"""
        )
        details, fingerprint, updated_at = cur.fetchone()

    assert details == {}
    assert fingerprint == ""
    assert updated_at is not None
