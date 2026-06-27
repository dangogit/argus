import pytest
from argus.v2.connectors.postgres import PostgresConnector, _ident
from argus.v2.config.schema import SourceRef


@pytest.fixture()
def source(pg_dsn):
    return SourceRef(name="tickets", type="postgres", scope="company", team="dev",
                     config={"dsn": pg_dsn, "table": "support_tickets",
                             "cursor_column": "id", "fingerprint_column": "id"})


def _seed(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS support_tickets "
                    "(id bigserial primary key, subject text)")
        cur.execute("TRUNCATE support_tickets RESTART IDENTITY")
        cur.execute("INSERT INTO support_tickets(subject) VALUES ('a'),('b'),('c')")
    conn.commit()


def test_polls_new_rows_then_nothing(conn, source):
    _seed(conn)
    signals, cursor = PostgresConnector().poll(source, {})
    assert [s.fingerprint for s in signals] == ["1", "2", "3"]
    assert cursor["last"] == 3
    signals2, _ = PostgresConnector().poll(source, cursor)
    assert signals2 == []


def test_new_row_after_cursor(conn, source):
    _seed(conn)
    _, cursor = PostgresConnector().poll(source, {})
    with conn.cursor() as cur:
        cur.execute("INSERT INTO support_tickets(subject) VALUES ('d')")
    conn.commit()
    signals, cursor2 = PostgresConnector().poll(source, cursor)
    assert [s.fingerprint for s in signals] == ["4"] and cursor2["last"] == 4


def test_identifier_validation_rejects_injection():
    with pytest.raises(ValueError):
        _ident("tickets; drop table x")


def test_timestamp_cursor(conn, pg_dsn):
    from argus.v2.config.schema import SourceRef
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS bug_rep "
                    "(id uuid primary key default gen_random_uuid(), "
                    "created_at timestamptz default now(), subject text)")
        cur.execute("TRUNCATE bug_rep")
        cur.execute("INSERT INTO bug_rep(subject) VALUES ('a'),('b')")
    conn.commit()
    src = SourceRef(name="bugs", type="postgres", scope="company", team="dev",
                    config={"dsn": pg_dsn, "table": "bug_rep", "cursor_column": "created_at",
                            "fingerprint_column": "id", "cursor_type": "timestamp"})
    sigs, cursor = PostgresConnector().poll(src, {})
    assert len(sigs) == 2
    sigs2, _ = PostgresConnector().poll(src, cursor)
    assert sigs2 == []


def test_quoted_prisma_columns_and_initial_cursor(conn, pg_dsn):
    with conn.cursor() as cur:
        cur.execute('CREATE TABLE IF NOT EXISTS webhook_errors '
                    '(id text primary key, "createdAt" timestamptz, "errorMessage" text)')
        cur.execute("TRUNCATE webhook_errors")
        cur.execute(
            'INSERT INTO webhook_errors(id, "createdAt", "errorMessage") VALUES '
            "('old', '2026-01-01T00:00:00+00:00', 'old'),"
            "('new', '2026-01-02T00:00:00+00:00', 'new')"
        )
    conn.commit()
    src = SourceRef(
        name="webhook-errors",
        type="postgres",
        scope="company",
        team="dev",
        config={
            "dsn": pg_dsn,
            "table": "webhook_errors",
            "cursor_column": "createdAt",
            "fingerprint_column": "id",
            "cursor_type": "timestamp",
            "initial_cursor": "2026-01-01T12:00:00+00:00",
        },
    )
    sigs, cursor = PostgresConnector().poll(src, {})
    assert [sig.fingerprint for sig in sigs] == ["new"]
    assert cursor["last"].startswith("2026-01-02T00:00:00")
