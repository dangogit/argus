"""Scoped knowledge add/search. Team sees company-level + its own. Semantic via
pgvector when an embedding of the matching dim exists, else keyword."""
from __future__ import annotations

import psycopg

from argus.v2.knowledge.embed import embed, vec_literal


def add(conn: psycopg.Connection, cfg, *, scope: str, team_id, title: str,
        content: str, source: str = "agent") -> str:
    lit = vec_literal(embed(content, cfg))
    tid = team_id if scope == "team" else None
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO knowledge (scope, team_id, title, content, embedding, source) "
            "VALUES (%s,%s,%s,%s,%s::vector,%s) RETURNING id",
            (scope, tid, title, content, lit, source))
        return str(cur.fetchone()[0])


def search(conn: psycopg.Connection, cfg, *, team_id, query: str, k: int = 5,
           sources: list[str] | tuple[str, ...] | None = None) -> list:
    source_filter = [str(s) for s in (sources or []) if str(s)]
    if sources is not None and not source_filter:
        return []
    qvec = embed(query, cfg)
    with conn.cursor() as cur:
        if qvec is not None:
            source_sql = " AND source = ANY(%s)" if source_filter else ""
            params = [team_id, len(qvec)]
            if source_filter:
                params.append(source_filter)
            params.append(vec_literal(qvec))
            params.append(k)
            cur.execute(
                "SELECT title, content FROM knowledge "
                "WHERE (scope='company' OR team_id=%s) AND embedding IS NOT NULL "
                "AND vector_dims(embedding)=%s "
                f"{source_sql} "
                "ORDER BY embedding <-> %s::vector LIMIT %s",
                params)
            rows = cur.fetchall()
            if rows:
                return [{"title": t, "content": c} for t, c in rows]
        source_sql = " AND source = ANY(%s)" if source_filter else ""
        params = [team_id, f"%{query}%"]
        if source_filter:
            params.append(source_filter)
        params.append(k)
        cur.execute(
            "SELECT title, content FROM knowledge "
            f"WHERE (scope='company' OR team_id=%s) AND content ILIKE %s{source_sql} "
            "LIMIT %s",
            params)
        return [{"title": t, "content": c} for t, c in cur.fetchall()]
