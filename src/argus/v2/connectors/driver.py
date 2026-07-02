"""Poll every configured source, ingest new signals, advance the cursor. One
transaction per source: a crash rolls back both ingest and cursor, so the next
poll re-fetches safely (fingerprint dedup makes re-ingest a no-op)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Json

import argus.v2.connectors  # noqa: F401  (registers connectors)
from argus.v2.connectors.base import REGISTRY
from argus.v2.connectors.client import classify
from argus.v2.ingress import events

log = logging.getLogger("argus.connectors")

# Exponential backoff for a failing source: 30s, 60s, 120s, ... capped at 1h.
# Stops a dead connector (expired key, provider outage) from re-polling every
# tick while still recovering on its own once the provider comes back.
_BACKOFF_BASE = 30.0
_BACKOFF_MAX = 3600.0


def _backoff_seconds(error_count: int, *, category: str = "transient") -> float:
    """Seconds to wait before the next poll after N consecutive failures.
    An auth failure (bad/expired key) will not fix itself on a retry, so it
    jumps straight to the max backoff instead of climbing the usual ladder -
    no point hammering a dead key every 30s for an hour before reaching it."""
    if error_count <= 0:
        return 0.0
    if category == "auth":
        return _BACKOFF_MAX
    return min(_BACKOFF_BASE * (2 ** (error_count - 1)), _BACKOFF_MAX)


@dataclass(frozen=True)
class PollPreview:
    source: str
    source_type: str
    team: str
    ok: bool
    count: int = 0
    cursor_keys: tuple[str, ...] = ()
    error_type: str = ""
    error_count: int = 0


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
            prefix = "AUTH " if status in (401, 403) else ""
            return f"{prefix}HTTPStatusError HTTP {status}{suffix}"
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


def _load_state(conn, source_name: str) -> dict:
    """Cursor plus failure/backoff metadata for one source."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cursor, error_count, poll_after FROM connector_state WHERE source_name=%s",
            (source_name,))
        row = cur.fetchone()
    if not row:
        return {"cursor": {}, "error_count": 0, "poll_after": None}
    return {"cursor": row[0] or {}, "error_count": row[1] or 0, "poll_after": row[2]}


def _record_failure(conn, source_name: str, exc: Exception, error_count: int) -> None:
    """Persist a connector failure and schedule the next poll after a backoff.
    Runs after the failed poll's transaction was rolled back, on a fresh write.
    An 'auth' category (bad/expired key) gets a distinct label and jumps straight
    to the max backoff; error_category persists so the orchestrator sweep can
    find it and alert without re-parsing the label text."""
    category = classify(exc)
    delay = _backoff_seconds(error_count, category=category)
    label = _error_label(exc)
    log.warning("connector %s failed (%d in a row, category=%s): %s; backing off %.0fs",
                source_name, error_count, category, label, delay)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO connector_state
                   (source_name, error_count, last_error, last_error_at, error_category,
                    poll_after, updated_at)
               VALUES (%s,%s,%s, now(), %s, now() + make_interval(secs => %s), now())
               ON CONFLICT (source_name) DO UPDATE SET
                   error_count=EXCLUDED.error_count, last_error=EXCLUDED.last_error,
                   last_error_at=EXCLUDED.last_error_at, error_category=EXCLUDED.error_category,
                   poll_after=EXCLUDED.poll_after, updated_at=now()""",
            (source_name, error_count, label, category, delay))


def _clear_failure(conn, source_name: str) -> None:
    """Reset failure/backoff state after a successful poll."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE connector_state
               SET error_count=0, last_error=NULL, error_category=NULL, poll_after=NULL,
                   updated_at=now()
               WHERE source_name=%s""",
            (source_name,))


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
              source_types: set[str] | None = None,
              now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    total = 0
    for source, team in _iter_sources(cfg):
        if source_names and source.name not in source_names:
            continue
        if source_types and source.type not in source_types:
            continue
        if source.type not in REGISTRY:
            continue
        state = _load_state(conn, source.name)
        if state["poll_after"] and state["poll_after"] > now:
            continue  # in backoff window after recent failures; skip this tick
        cursor = state["cursor"]
        connector = REGISTRY[source.type]()
        try:
            signals, new_cursor = connector.poll(source, cursor)
        except Exception as exc:
            conn.rollback()  # a failing source must not block the others
            _record_failure(conn, source.name, exc, state["error_count"] + 1)
            conn.commit()
            continue
        for sig in signals:
            events.ingest_signal(conn, cfg, team=team, source=source.name,
                                 fingerprint=sig.fingerprint, payload=sig.payload)
            total += 1
        _save_cursor(conn, source.name, new_cursor)
        if state["error_count"]:
            _clear_failure(conn, source.name)  # recovered: reset backoff
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
        state = _load_state(conn, source.name)
        cursor = state["cursor"]
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
                error_count=state["error_count"] + 1,
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
