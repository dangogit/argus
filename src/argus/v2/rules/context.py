"""Deterministic owner-rule context for workers.

Rules are stored in `knowledge` so the existing CLI, remember action, and
support guidance path can all feed the same source of truth.
"""
from __future__ import annotations

import psycopg

RULE_SOURCES = ("owner-rule", "support-rule")


def rows(conn: psycopg.Connection, *, team_id: str, limit: int = 20,
         sources: tuple[str, ...] = RULE_SOURCES) -> list[dict[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT title, content
            FROM knowledge
            WHERE (scope='company' OR team_id=%s)
              AND source = ANY(%s)
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            (team_id, list(sources), int(limit)),
        )
        out = []
        seen: set[str] = set()
        for title, content in cur.fetchall():
            key = " ".join(str(content or "").split()).lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append({"title": str(title or "rule"), "content": str(content or "")})
        return out


def block_for(conn: psycopg.Connection | None, _cfg, *, team_id: str,
              limit: int = 20) -> str:
    if conn is None:
        return ""
    try:
        items = rows(conn, team_id=team_id, limit=limit)
    except Exception:
        return ""
    if not items:
        return ""
    lines = ["OWNER RULES (apply before worker memory or preference):"]
    lines.extend(f"- {item['title']}: {item['content']}" for item in items)
    return "\n".join(lines)
