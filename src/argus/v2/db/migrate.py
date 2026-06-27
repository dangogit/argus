"""Apply ordered *.sql migrations once each, tracked in schema_migrations."""
from pathlib import Path
from typing import List

import psycopg


def apply(conn: psycopg.Connection, migrations_dir: Path) -> List[str]:
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        cur.execute("SELECT version FROM schema_migrations")
        done = {r[0] for r in cur.fetchall()}
    applied: List[str] = []
    for sql_file in sorted(Path(migrations_dir).glob("*.sql")):
        version = sql_file.stem
        if version in done:
            continue
        with conn.cursor() as cur:
            cur.execute(sql_file.read_text(encoding="utf-8"))
            cur.execute("INSERT INTO schema_migrations(version) VALUES (%s)", (version,))
        applied.append(version)
    conn.commit()
    return applied
