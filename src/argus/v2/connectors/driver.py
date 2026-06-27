"""Poll every configured source, ingest new signals, advance the cursor. One
transaction per source: a crash rolls back both ingest and cursor, so the next
poll re-fetches safely (fingerprint dedup makes re-ingest a no-op)."""
from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg.types.json import Json

import argus.v2.connectors  # noqa: F401  (registers connectors)
from argus.v2.connectors.base import REGISTRY
from argus.v2.ingress import events


@dataclass(frozen=True)
class PollPreview:
    source: str
    source_type: str
    team: str
    ok: bool
    count: int = 0
    cursor_keys: tuple[str, ...] = ()
    error_type: str = ""


def _error_label(exc: Exception) -> str:
    try:
        import httpx
        if isinstance(exc, httpx.HTTPStatusError):
            response = exc.response
            status = response.status_code
            message = ""
            try:
                body = response.json()
                error = body.get("error") if isinstance(body, dict) else None
                if isinstance(error, dict):
                    message = str(error.get("message") or error.get("code") or "")
            except ValueError:
                message = ""
            suffix = f" {message}" if message else ""
            return f"HTTPStatusError HTTP {status}{suffix}"
    except Exception:
        pass
    return type(exc).__name__


def _iter_sources(cfg):
    for s in cfg.company.sources:
        team = s.team
        if team:
            yield s, team
    for t in cfg.teams:
        for s in t.sources:
            yield s, t.name


def _load_cursor(conn, source_name: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT cursor FROM connector_state WHERE source_name=%s", (source_name,))
        row = cur.fetchone()
    return row[0] if row else {}


def _save_cursor(conn, source_name: str, cursor: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO connector_state (source_name, cursor, last_polled_at)
               VALUES (%s,%s, now())
               ON CONFLICT (source_name)
               DO UPDATE SET cursor=EXCLUDED.cursor, last_polled_at=now(), updated_at=now()""",
            (source_name, Json(cursor)))


def poll_once(conn: psycopg.Connection, cfg, *,
              source_names: set[str] | None = None,
              source_types: set[str] | None = None) -> int:
    total = 0
    for source, team in _iter_sources(cfg):
        if source_names and source.name not in source_names:
            continue
        if source_types and source.type not in source_types:
            continue
        if source.type not in REGISTRY:
            continue
        cursor = _load_cursor(conn, source.name)
        connector = REGISTRY[source.type]()
        try:
            signals, new_cursor = connector.poll(source, cursor)
        except Exception:
            conn.rollback()
            continue  # a failing source must not block the others
        for sig in signals:
            events.ingest_signal(conn, cfg, team=team, source=source.name,
                                 fingerprint=sig.fingerprint, payload=sig.payload)
            total += 1
        _save_cursor(conn, source.name, new_cursor)
        conn.commit()
    return total


def dry_run(conn: psycopg.Connection, cfg, *,
            source_names: set[str] | None = None,
            source_types: set[str] | None = None) -> list[PollPreview]:
    previews: list[PollPreview] = []
    for source, team in _iter_sources(cfg):
        if source_names and source.name not in source_names:
            continue
        if source_types and source.type not in source_types:
            continue
        if source.type not in REGISTRY:
            continue
        cursor = _load_cursor(conn, source.name)
        connector = REGISTRY[source.type]()
        try:
            signals, new_cursor = connector.poll(source, cursor)
        except Exception as exc:
            conn.rollback()
            previews.append(PollPreview(
                source=source.name,
                source_type=source.type,
                team=team,
                ok=False,
                error_type=_error_label(exc),
            ))
            continue
        conn.rollback()
        previews.append(PollPreview(
            source=source.name,
            source_type=source.type,
            team=team,
            ok=True,
            count=len(signals),
            cursor_keys=tuple(sorted(new_cursor.keys())),
        ))
    return previews
