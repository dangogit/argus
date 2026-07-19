from __future__ import annotations

from typing import Any

import psycopg

from argus.v2.ownership import store


_DEFINITION_OF_DONE = {
    "code": {"pr": True},
    "support": {"reply": True},
    "maintenance": {"healthy": True},
}


def _enabled(cfg, team_id: str | None) -> bool:
    if not team_id:
        return False
    try:
        return bool(cfg.team(team_id).ownership.enabled)
    except KeyError:
        return False


def _team_for_request(conn: psycopg.Connection, request_id) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT team_id FROM requests WHERE id=%s", (request_id,))
        row = cur.fetchone()
    return str(row[0]) if row else None


def _item_for_request(conn: psycopg.Connection, request_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM team_obligations WHERE request_id=%s",
            (request_id,),
        )
        row = cur.fetchone()
    return store.get(conn, row[0]) if row else None


def _request_hook_item(conn, cfg, *, request_id, team_id):
    resolved_team_id = team_id or _team_for_request(conn, request_id)
    if not _enabled(cfg, resolved_team_id):
        return None
    return _item_for_request(conn, request_id)


def _title(payload: dict[str, Any], kind: str, dedup_key: str) -> str:
    for field in ("text", "title", "message", "summary"):
        value = payload.get(field)
        if value:
            return " ".join(str(value).split())[:500]
    return f"{kind} obligation for {dedup_key}"


def existing_nonterminal_request_for_event(
    conn: psycopg.Connection,
    cfg,
    *,
    event_id,
    team_id,
) -> str | None:
    if not _enabled(cfg, team_id):
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.request_id
            FROM events e
            JOIN team_obligations o
              ON o.team_id=%s
             AND o.fingerprint='event:' || e.dedup_key
            WHERE e.id=%s
              AND o.status NOT IN ('done', 'failed')
              AND o.request_id IS NOT NULL
            """,
            (team_id, event_id),
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


def open_for_request(
    conn: psycopg.Connection,
    cfg,
    *,
    request_id,
    event_id,
    team_id,
    kind="code",
):
    if not _enabled(cfg, team_id):
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT dedup_key, payload FROM events WHERE id=%s",
            (event_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    dedup_key, payload = row
    item = store.upsert(
        conn,
        team_id=team_id,
        kind=kind,
        fingerprint=f"event:{dedup_key}",
        title=_title(dict(payload or {}), kind, str(dedup_key)),
        source_ref=f"event:{event_id}",
        definition_of_done=dict(_DEFINITION_OF_DONE[kind]),
    )
    return store.link_request(conn, item.id, request_id)


def on_request_working(
    conn: psycopg.Connection,
    cfg,
    *,
    request_id,
    team_id=None,
):
    item = _request_hook_item(
        conn, cfg, request_id=request_id, team_id=team_id,
    )
    if item is None:
        return None
    return store.transition(
        conn,
        item.id,
        to_status="working",
        reason="pipeline stage zero enqueued",
        evidence={"request_id": str(request_id)},
    )


def on_pr_proposed(
    conn: psycopg.Connection,
    cfg,
    *,
    request_id,
    action_id,
    team_id=None,
):
    item = _request_hook_item(
        conn, cfg, request_id=request_id, team_id=team_id,
    )
    if item is None:
        return None
    store.link_action(conn, item.id, action_id)
    return store.transition(
        conn,
        item.id,
        to_status="awaiting_pr",
        reason="open_pr action proposed",
        evidence={"action_id": str(action_id)},
    )


def on_request_blocked(
    conn: psycopg.Connection,
    cfg,
    *,
    request_id,
    reason,
    classification,
    team_id=None,
):
    item = _request_hook_item(
        conn, cfg, request_id=request_id, team_id=team_id,
    )
    if item is None:
        return None
    return store.transition(
        conn,
        item.id,
        to_status="blocked",
        reason=reason,
        evidence={
            "request_id": str(request_id),
            "classification": classification,
            "reason": reason,
        },
    )
