"""Generic table poller: emit one signal per new row beyond a cursor column.
Fully gate-tested against the test Postgres. Identifiers are allowlist-validated
(no injection); the cursor value is parameterized."""
from __future__ import annotations

import re
from datetime import timezone

import psycopg
from psycopg import sql

from argus.v2.connectors.base import Signal, register

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _ident(name: str) -> str:
    if not _IDENT.match(name or ""):
        raise ValueError(f"unsafe identifier: {name!r}")
    return name


def _initial_cursor(cfg: dict, ctype: str):
    if "initial_cursor" in cfg:
        value = cfg["initial_cursor"]
        return int(value) if ctype == "int" else value
    return "1970-01-01T00:00:00+00:00" if ctype == "timestamp" else 0


def _cursor_value(value, ctype: str):
    if ctype != "timestamp" or not hasattr(value, "isoformat"):
        return value
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


@register
class PostgresConnector:
    type = "postgres"

    def poll(self, source, state: dict):
        cfg = source.config or {}
        table = sql.Identifier(_ident(cfg["table"]))
        cursor_col = sql.Identifier(_ident(cfg["cursor_column"]))
        fp_col = sql.Identifier(_ident(cfg.get("fingerprint_column", cfg["cursor_column"])))
        ctype = cfg.get("cursor_type", "int")  # "int" | "timestamp"
        page = int(cfg.get("page_size", 200))
        last = state.get("last")
        if last is None:
            last = _initial_cursor(cfg, ctype)
        dsn = cfg.get("dsn") or source.secret
        with psycopg.connect(dsn) as c, c.cursor() as cur:
            query = sql.SQL(
                "SELECT {fp_col}, {cursor_col}, to_jsonb(t) FROM {table} t "
                "WHERE {cursor_col} > %s ORDER BY {cursor_col} LIMIT %s"
            ).format(table=table, cursor_col=cursor_col, fp_col=fp_col)
            cur.execute(query, (last, page))
            rows = cur.fetchall()
        signals = [Signal(fingerprint=str(fp), payload=row) for fp, _cval, row in rows]
        if rows:
            # rows are ORDER BY cursor ASC, so the last row holds the new cursor.
            cval = rows[-1][1]
            last = _cursor_value(cval, ctype)
        return signals, {"last": last}
