"""Supabase PostgREST connector for open bug or feedback rows.

This is the Python v2 replacement for the legacy shell gatherer. It emits the
same normalized finding fields while keeping a source-local seen set so open
rows do not re-enter the durable event inbox every poll.

Incremental cursor: the connector persists the max value of ``cursor_column``
(default ``created_at``) it has emitted and fetches only rows newer than that
watermark (PostgREST ``cursor_column=gt.<watermark>`` + ``order=...asc``). This
is the primary dedup guard: a standing-match table (e.g.
``transactions?status=eq.failed``) is not re-pulled in full every poll, and
already-old rows are excluded server-side even if the in-memory ``seen`` set is
lost (restart / state reset). ``seen`` stays as a secondary client-side guard.
Tables without ``cursor_column`` fall back to the old seen-only behavior.
"""
from __future__ import annotations

import re
from urllib.parse import quote, urlencode

from argus.v2.connectors.base import Signal, register

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SEEN_CAP = 1000
_DEFAULT_CURSOR_COLUMN = "created_at"


def _name(value: str) -> str:
    if not _NAME.match(value or ""):
        raise ValueError(f"unsafe name: {value!r}")
    return value


def _cursor_column(cfg: dict) -> str | None:
    """Column used as the incremental watermark. ``cursor_column: ""`` (or null)
    disables the server-side cursor for tables that have no monotonic column.
    Validated as an identifier because it is interpolated into the query."""
    col = cfg.get("cursor_column", _DEFAULT_CURSOR_COLUMN)
    if not col:
        return None
    return _name(col)


def _severity(value) -> str:
    s = str(value or "").lower()
    if s in ("critical", "fatal"):
        return "critical"
    if s in ("error", "high"):
        return "error"
    if s in ("warn", "warning", "medium"):
        return "warn"
    if s in ("info", "low"):
        return "info"
    return "warn"


def _build_url(base: str, table: str, query: str, limit: int, *,
               cursor_column: str | None = None, watermark=None) -> str:
    """Compose the PostgREST URL. With a cursor column, order ascending and (when
    a watermark exists) only pull rows strictly newer than it."""
    parts: list[str] = []
    if query:
        parts.append(query)
    if cursor_column and watermark is not None:
        parts.append(f"{cursor_column}=gt.{quote(str(watermark), safe='')}")
    if cursor_column:
        parts.append(f"order={cursor_column}.asc")
    parts.append(urlencode({"limit": limit}))
    return f"{base}/rest/v1/{table}?" + "&".join(parts)


@register
class SupabaseConnector:
    type = "supabase"

    @staticmethod
    def parse(raw, state: dict, *, project: str, table: str = "bug_reports",
              id_column: str = "id", title_column: str = "title",
              severity_column: str | None = None,
              cursor_column: str | None = _DEFAULT_CURSOR_COLUMN):
        seen = set(state.get("seen", []))
        next_seen = list(state.get("seen", []))
        watermark = state.get("watermark")
        signals: list[Signal] = []
        rows = raw if isinstance(raw, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            rid = str(row.get(id_column, "?"))
            fingerprint = f"supabase-{project}-{table}-{rid}"
            cur_val = row.get(cursor_column) if cursor_column else None
            # Primary guard: drop rows at or before the watermark even if the
            # server filter wasn't applied (column missing, state reset feeding
            # old rows). The server uses strict gt; ties at the exact watermark
            # are caught by `seen` below so a real new row is never lost.
            # ponytail: lexical compare assumes ISO-8601/monotonic cursor_column
            # (created_at, ulid). Numeric ids would mis-sort; set cursor_column
            # accordingly or "" to disable.
            if cur_val is not None and watermark is not None \
                    and str(cur_val) < str(watermark):
                continue
            if fingerprint in seen:
                continue
            title = (
                row.get(title_column)
                or row.get("description")
                or row.get("message")
                or row.get("title")
                or f"row {rid}"
            )
            finding = {
                "source": "supabase",
                "severity": _severity(row.get(severity_column)) if severity_column else "warn",
                "fingerprint": fingerprint,
                "message": str(title),
                "url": row.get("url") or "",
                "kind": "bug",
                "last_seen": row.get("created_at") or row.get("updated_at") or row.get("inserted_at") or "",
                "row": row,
            }
            signals.append(Signal(fingerprint=fingerprint, payload=finding))
            seen.add(fingerprint)
            next_seen.append(fingerprint)
            # Advance the durable watermark to the newest cursor value emitted.
            if cur_val is not None and (watermark is None or str(cur_val) > str(watermark)):
                watermark = cur_val
        new_state = {"seen": next_seen[-_SEEN_CAP:]}
        if watermark is not None:
            new_state["watermark"] = watermark
        return signals, new_state

    def fetch(self, source, state: dict):  # pragma: no cover
        import httpx
        cfg = source.config or {}
        base = (cfg.get("url") or "").rstrip("/")
        key = source.secret
        if not base or not key:
            return []
        table = _name(cfg.get("table", "bug_reports"))
        query = cfg.get("filter", "status=eq.open")
        limit = int(cfg.get("limit", 25))
        col = _cursor_column(cfg)
        headers = {"apikey": key, "Authorization": f"Bearer {key}"}
        timeout = float(cfg.get("timeout", 15))
        url = _build_url(base, table, query, limit,
                         cursor_column=col, watermark=state.get("watermark"))
        try:
            r = httpx.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Table has no cursor_column (PostgREST 42703): fall back to the
            # un-ordered, un-cursored fetch so the source keeps working on the
            # seen-set guard alone.
            if col and _missing_column(exc):
                r = httpx.get(_build_url(base, table, query, limit),
                              headers=headers, timeout=timeout)
                r.raise_for_status()
            else:
                raise
        data = r.json()
        return data if isinstance(data, list) else []

    def poll(self, source, state: dict):
        cfg = source.config or {}
        table = _name(cfg.get("table", "bug_reports"))
        return self.parse(
            self.fetch(source, state),
            state,
            project=cfg.get("project") or source.team or source.name,
            table=table,
            id_column=cfg.get("id_column", "id"),
            title_column=cfg.get("title_column", "title"),
            severity_column=cfg.get("severity_column"),
            cursor_column=_cursor_column(cfg),
        )


def _missing_column(exc) -> bool:  # pragma: no cover
    """True if a PostgREST error is an undefined-column (42703) complaint."""
    resp = getattr(exc, "response", None)
    if resp is None or resp.status_code not in (400, 404):
        return False
    body = resp.text or ""
    return "42703" in body or "does not exist" in body.lower()
