"""Append-only support draft state compatible with the legacy support registry."""
from __future__ import annotations

import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from argus.v2.db import pool


@dataclass(frozen=True)
class Draft:
    id: str
    path: Path


@dataclass(frozen=True)
class Guidance:
    id: str
    path: Path


def support_root() -> Path:
    explicit = os.environ.get("ARGUS_SUPPORT_DIR")
    if explicit:
        return Path(explicit)
    artifact_root = os.environ.get("ARGUS_ARTIFACT_DIR", str(Path.cwd() / "run"))
    return Path(artifact_root) / "support"


def project_dir(project: str) -> Path:
    path = support_root() / project
    return path


def latest_action(project: str, thread_id: str) -> str:
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT action
                FROM support_thread_events
                WHERE project=%s AND thread_id=%s
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (project, thread_id),
            )
            row = cur.fetchone()
    return str(row[0]) if row else ""


def has_ready_draft(project: str, thread_id: str) -> bool:
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM support_drafts
                WHERE project=%s AND thread_id=%s AND status='ready'
                LIMIT 1
                """,
                (project, thread_id),
            )
            return cur.fetchone() is not None


def draft_get(project: str, draft_id: str) -> dict | None:
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, project, thread_id, sender, subject, reply, transport,
                       status, created_at, updated_at
                FROM support_drafts
                WHERE project=%s AND id=%s
                """,
                (project, draft_id),
            )
            row = cur.fetchone()
    return _draft_row(row) if row else None


def draft_list(project: str | None = None, *, status: str | None = "ready",
               limit: int = 50) -> list[dict]:
    clauses = []
    params: list[object] = []
    if project:
        clauses.append("project=%s")
        params.append(project)
    if status:
        clauses.append("status=%s")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, project, thread_id, sender, subject, reply, transport,
                       status, created_at, updated_at
                FROM support_drafts
                {where}
                ORDER BY updated_at DESC, created_at DESC
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()
    return [_draft_row(row) for row in rows]


def draft_set_status(project: str, draft_id: str, status: str) -> None:
    if status not in {"ready", "sent", "cleared", "failed"}:
        raise ValueError(f"invalid draft status: {status}")
    draft = draft_get(project, draft_id)
    if not draft:
        raise ValueError(f"unknown support draft: {draft_id}")
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE support_drafts
                SET status=%s, updated_at=clock_timestamp()
                WHERE project=%s AND id=%s
                """,
                (status, project, draft_id),
            )
        conn.commit()


def record(project: str, thread_id: str, action: str, sender: str, subject: str,
           reason: str = "") -> None:
    if action not in {
        "replied", "archived", "escalated", "failed", "needs_human",
        "draft_ready", "guidance_requested", "guidance_answered",
        "guidance_rejected", "auto_replied",
    }:
        raise ValueError(f"invalid support action: {action}")
    now = datetime.now(timezone.utc)
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO support_thread_events
                  (project, thread_id, action, sender, subject, reason, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    project,
                    thread_id,
                    action,
                    sender,
                    subject,
                    reason,
                    now,
                ),
            )
        conn.commit()


def register_draft(project: str, thread_id: str, sender: str, subject: str,
                   reply: str, transport: str) -> Draft:
    draft_id = _new_id()
    pdir = project_dir(project)
    ddir = pdir / "drafts" / draft_id
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO support_drafts
                  (id, project, thread_id, sender, subject, reply, transport, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'ready')
                """,
                (draft_id, project, thread_id, sender, subject, reply, transport),
            )
        conn.commit()
    record(project, thread_id, "draft_ready", sender, subject)
    return Draft(id=draft_id, path=ddir)


def register_guidance_request(project: str, thread_id: str, sender: str,
                              subject: str, question: str, proposed_reply: str,
                              thread: str) -> Guidance:
    gid = _new_id()
    pdir = project_dir(project)
    gdir = pdir / "guidance" / gid
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO support_guidance
                  (id, project, thread_id, sender, subject, question,
                   proposed_reply, thread_text, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'pending')
                """,
                (gid, project, thread_id, sender, subject, question, proposed_reply, thread),
            )
        conn.commit()
    record(project, thread_id, "guidance_requested", sender, subject)
    return Guidance(id=gid, path=gdir)


def guidance_request(project: str, guidance_id: str) -> dict | None:
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, project, thread_id, sender, subject, question,
                       proposed_reply, thread_text, status
                FROM support_guidance
                WHERE project=%s AND id=%s
                """,
                (project, guidance_id),
            )
            row = cur.fetchone()
    if not row:
        return None
    gid, project, thread_id, sender, subject, question, proposed_reply, thread, status = row
    return {
        "id": str(gid),
        "project": project,
        "thread_id": thread_id,
        "from": sender,
        "subject": subject,
        "question": question,
        "proposed_reply": proposed_reply,
        "thread": thread,
        "status": status,
    }


def resolve_guidance(project: str, guidance_id: str, answer: str,
                     *, status: str) -> None:
    req = guidance_request(project, guidance_id) or {}
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE support_guidance
                SET status=%s, answer=%s, updated_at=clock_timestamp()
                WHERE project=%s AND id=%s
                """,
                (status, answer, project, guidance_id),
            )
        conn.commit()
    action = "guidance_rejected" if status == "rejected" else "guidance_answered"
    if req.get("thread_id"):
        record(project, req["thread_id"], action, req.get("from", ""), req.get("subject", ""))


def accepted_guidance_count(project: str) -> int:
    with pool.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*)
                FROM support_guidance
                WHERE project=%s AND status IN ('answered','sent','auto_sent')
                """,
                (project,),
            )
            return int(cur.fetchone()[0])


def _new_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{random.randint(0, 99999):05d}"


def _draft_row(row) -> dict:
    draft_id, project, thread_id, sender, subject, reply, transport, status, created_at, updated_at = row
    return {
        "id": str(draft_id),
        "project": project,
        "thread_id": thread_id,
        "sender": sender,
        "subject": subject,
        "reply": reply,
        "transport": transport,
        "status": status,
        "created_at": _ts(created_at),
        "updated_at": _ts(updated_at),
    }


def _ts(value) -> str:
    if hasattr(value, "astimezone"):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value or "")
