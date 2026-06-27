"""Usage extraction from a hermes profile's state.db.

Read-only and fail-open: cost provenance is best-effort decoration, a broken
or missing DB must never fail an engine call. Schema observed at hermes-agent
e71d746: sessions(id, source, started_at, ..., estimated_cost_usd).
"""
import sqlite3
from pathlib import Path
from typing import Optional


def last_session_cost(home: Path) -> Optional[str]:
    db_path = home / "state.db"
    if not db_path.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT estimated_cost_usd FROM sessions"
                " WHERE source = 'cli' ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not row or row[0] is None:
        return None
    return str(row[0])
