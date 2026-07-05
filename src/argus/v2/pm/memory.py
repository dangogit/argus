"""Postgres-backed project lessons for PM runs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import psycopg

_OUTCOMES = frozenset({
    "proposed", "qa-pass", "qa-fail", "no-change", "blocked", "found-not-fixed",
})
_ATTR_OUTCOMES = frozenset({"qa-pass", "qa-fail"})
_MIN_APPLIED = 5
_MIN_FAILS = 3
_FAIL_RATE = 0.5


@dataclass(frozen=True)
class Lesson:
    fingerprint: str
    finding: str
    outcome: str
    note: str


def append(conn: psycopg.Connection, *, team_id: str, fingerprint: str,
           finding: str, outcome: str, note: str = "") -> None:
    if outcome not in _OUTCOMES:
        raise ValueError(f"invalid memory outcome: {outcome}")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pm_lessons (team_id, fingerprint, finding, outcome, note, created_at)
            VALUES (%s,%s,%s,%s,%s,clock_timestamp())
            """,
            (team_id, fingerprint, finding, outcome, note),
        )


def attribute(conn: psycopg.Connection, *, team_id: str, request_id: str,
              fingerprints: Iterable[str], outcome: str,
              own_fingerprint: str | None = None) -> None:
    if outcome not in _ATTR_OUTCOMES:
        return
    seen: set[str] = set()
    with conn.cursor() as cur:
        for fp in fingerprints:
            if not fp or fp == own_fingerprint or fp in seen:
                continue
            seen.add(fp)
            cur.execute(
                """
                INSERT INTO pm_lesson_attributions
                  (team_id, request_id, lesson_fingerprint, outcome)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (team_id, request_id, lesson_fingerprint) DO NOTHING
                """,
                (team_id, request_id, fp, outcome),
            )


def selected(conn: psycopg.Connection, *, team_id: str, limit: int = 20) -> list[Lesson]:
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH latest AS (
              SELECT DISTINCT ON (fingerprint)
                     id, fingerprint, finding, outcome, note, created_at
              FROM pm_lessons
              WHERE team_id=%s
              ORDER BY fingerprint, created_at DESC, id DESC
            ), stats AS (
              SELECT lesson_fingerprint AS fingerprint,
                     count(*) FILTER (WHERE outcome='qa-pass') AS pass,
                     count(*) FILTER (WHERE outcome='qa-fail') AS fail
              FROM pm_lesson_attributions
              WHERE team_id=%s
              GROUP BY lesson_fingerprint
            )
            SELECT l.fingerprint, l.finding, l.outcome, l.note
            FROM latest l
            LEFT JOIN stats s USING (fingerprint)
            WHERE NOT (
              COALESCE(s.pass, 0) + COALESCE(s.fail, 0) >= %s
              AND COALESCE(s.fail, 0) >= %s
              AND (COALESCE(s.fail, 0)::float
                   / NULLIF(COALESCE(s.pass, 0) + COALESCE(s.fail, 0), 0)) > %s
            )
            ORDER BY l.created_at DESC, l.id DESC
            LIMIT %s
            """,
            (team_id, team_id, _MIN_APPLIED, _MIN_FAILS, _FAIL_RATE, limit),
        )
        return [Lesson(*row) for row in cur.fetchall()]


def render(conn: psycopg.Connection, *, team_id: str, limit: int = 20) -> str:
    rows = selected(conn, team_id=team_id, limit=limit)
    if not rows:
        return ""
    lines = [
        "## Project memory (recent, read-only)",
        *[
            f"- [fp {r.fingerprint}] {_rendered_outcome(r)}: {r.finding}"
            + (f" :: {r.note}" if r.note else "")
            for r in rows
        ],
        "Use this to avoid repeating rejected approaches and to build on prior fixes.",
    ]
    return "\n".join(lines)


def _rendered_outcome(lesson: Lesson) -> str:
    if lesson.outcome == "qa-fail" and lesson.note.startswith("Environment blocker:"):
        return "environment-blocker"
    return lesson.outcome


def fingerprints(conn: psycopg.Connection, *, team_id: str, limit: int = 20) -> list[str]:
    return [row.fingerprint for row in selected(conn, team_id=team_id, limit=limit)]


def record_request_outcome(conn: psycopg.Connection, *, request_id: str,
                           outcome: str, note: str = "") -> None:
    if outcome not in _OUTCOMES:
        raise ValueError(f"invalid memory outcome: {outcome}")
    team_id, fingerprint, finding = _request_info(conn, request_id)
    append(conn, team_id=team_id, fingerprint=fingerprint, finding=finding,
           outcome=outcome, note=note)
    if outcome in _ATTR_OUTCOMES:
        attribute(conn, team_id=team_id, request_id=request_id,
                  fingerprints=_injected_fingerprints(conn, request_id),
                  outcome=outcome, own_fingerprint=fingerprint)


def _request_info(conn: psycopg.Connection, request_id: str) -> tuple[str, str, str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.team_id, COALESCE(r.fingerprint, r.id::text),
                   COALESCE(e.payload->>'text', e.payload->>'message',
                            e.payload->>'title', r.fingerprint, r.id::text)
            FROM requests r
            JOIN events e ON e.id = r.event_id
            WHERE r.id=%s
            """,
            (request_id,),
        )
        row = cur.fetchone()
    if not row:
        raise ValueError(f"unknown request: {request_id}")
    return row[0], row[1], row[2]


def _injected_fingerprints(conn: psycopg.Connection, request_id: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT result->'memory_fingerprints'
            FROM jobs
            WHERE request_id=%s AND result ? 'memory_fingerprints'
            ORDER BY updated_at
            """,
            (request_id,),
        )
        rows = cur.fetchall()
    out: list[str] = []
    for (value,) in rows:
        if isinstance(value, list):
            out.extend(str(v) for v in value if v)
    return out
