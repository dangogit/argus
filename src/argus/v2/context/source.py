"""Context source adapters for pure Python v2 jobs."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


class SourceError(Exception):
    """Raised when a configured context source cannot be read."""


@dataclass(frozen=True)
class SourceRow:
    id: int
    who: str
    body: str
    ts: str


_BARE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def fetch(driver: str, since: int, limit: int) -> list[SourceRow]:
    driver = driver or "sqlite"
    since = since if isinstance(since, int) and since >= 0 else 0
    limit = limit if isinstance(limit, int) and limit >= 0 else 200
    if driver == "echo":
        return _fetch_echo(since, limit)
    if driver == "sqlite":
        return _fetch_sqlite(since, limit)
    raise SourceError(f"unknown context source driver: {driver}")


def _fetch_echo(since: int, limit: int) -> list[SourceRow]:
    rows: list[SourceRow] = []
    raw = os.environ.get("ARGUS_CONTEXT_ECHO_ROWS", "")
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            row_id = int(item.get("id"))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if row_id <= since:
            continue
        rows.append(SourceRow(
            id=row_id,
            who=str(item.get("who") or ""),
            body=str(item.get("body") or ""),
            ts=str(item.get("ts") or ""),
        ))
    rows.sort(key=lambda row: row.id)
    return rows[:limit]


def _bare(value: str, label: str) -> str:
    if not _BARE.match(value or ""):
        raise SourceError(f"context sqlite: {label} must be a bare identifier")
    return value


def _fetch_sqlite(since: int, limit: int) -> list[SourceRow]:
    db = os.environ.get("ARGUS_CONTEXT_DB", "")
    table = os.environ.get("ARGUS_CONTEXT_TABLE", "")
    if not db:
        raise SourceError("context sqlite: ARGUS_CONTEXT_DB unset")
    # Resolve $HOME explicitly: launchd jobs don't always export HOME, so
    # os.path.expandvars would leave "$HOME" literal and the path would never
    # resolve. Path.home() falls back to the passwd db when HOME is unset.
    home = str(Path.home())
    raw = db.replace("${HOME}", home).replace("$HOME", home)
    db_path = Path(os.path.expandvars(raw)).expanduser()
    if not db_path.is_file():
        raise SourceError(f"context sqlite: DB not readable: {db_path}")
    if not table:
        raise SourceError("context sqlite: ARGUS_CONTEXT_TABLE unset")

    tbl = _bare(table, "ARGUS_CONTEXT_TABLE")
    col_id = _bare(os.environ.get("ARGUS_CONTEXT_COL_ID", "id"), "ARGUS_CONTEXT_COL_ID")
    col_body = _bare(os.environ.get("ARGUS_CONTEXT_COL_BODY", "body"), "ARGUS_CONTEXT_COL_BODY")
    col_ts = _bare(os.environ.get("ARGUS_CONTEXT_COL_TS", "wa_timestamp"), "ARGUS_CONTEXT_COL_TS")
    col_recv = _bare(os.environ.get("ARGUS_CONTEXT_COL_RECEIVED", "received_at"),
                     "ARGUS_CONTEXT_COL_RECEIVED")
    who_expr = os.environ.get("ARGUS_CONTEXT_WHO_EXPR")
    if not who_expr:
        who_expr = _bare(os.environ.get("ARGUS_CONTEXT_COL_WHO", "participant_name"),
                         "ARGUS_CONTEXT_COL_WHO")

    resolved = db_path.resolve()
    uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"
    sql = (
        f"SELECT {col_id} AS id, ({who_expr}) AS who, {col_body} AS body, "
        f"COALESCE({col_ts}, {col_recv}) AS ts "
        f"FROM {tbl} WHERE {col_id} > ? ORDER BY {col_id} ASC LIMIT ?"
    )
    rows: list[SourceRow] = []
    try:
        with sqlite3.connect(uri, uri=True, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            for item in conn.execute(sql, (since, limit)):
                rows.append(SourceRow(
                    id=int(item["id"]),
                    who=str(item["who"] or ""),
                    body=str(item["body"] or ""),
                    ts=str(item["ts"] or ""),
                ))
    except sqlite3.Error as exc:
        raise SourceError("context sqlite: fetch failed") from exc
    return rows
