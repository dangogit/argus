"""Supabase PostgREST connector for open bug or feedback rows.

This is the Python v2 replacement for the legacy shell gatherer. It emits the
same normalized finding fields while keeping a source-local seen set so open
rows do not re-enter the durable event inbox every poll.
"""
from __future__ import annotations

import re
from urllib.parse import urlencode

from argus.v2.connectors.base import Signal, register

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SEEN_CAP = 1000


def _name(value: str) -> str:
    if not _NAME.match(value or ""):
        raise ValueError(f"unsafe name: {value!r}")
    return value


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


@register
class SupabaseConnector:
    type = "supabase"

    @staticmethod
    def parse(raw, state: dict, *, project: str, table: str = "bug_reports",
              id_column: str = "id", title_column: str = "title",
              severity_column: str | None = None):
        seen = set(state.get("seen", []))
        next_seen = list(state.get("seen", []))
        signals: list[Signal] = []
        rows = raw if isinstance(raw, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            rid = str(row.get(id_column, "?"))
            fingerprint = f"supabase-{project}-{table}-{rid}"
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
        return signals, {"seen": next_seen[-_SEEN_CAP:]}

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
        sep = "&" if query else ""
        url = f"{base}/rest/v1/{table}?{query}{sep}{urlencode({'limit': limit})}"
        r = httpx.get(
            url,
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=float(cfg.get("timeout", 15)),
        )
        r.raise_for_status()
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
        )
