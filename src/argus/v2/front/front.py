"""Conversational front. Slice 1 ships a deterministic rule front: if the
message asks for work (an imperative/work verb), dispatch a pipeline request;
otherwise emit a reply action. An LLM-driven front (manager role, cheap triage
model) replaces `decide` when real engines/channels land in slice 4 - the seam
is this one function.

Engine-driven seam (L4): when the event payload carries `_front_result`, that
dict drives the decision deterministically (used by tests and the scripted
engine). In production, the manager role engine would be called here; on any
failure we fall through to the rule."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import psycopg

from argus.v2.memory import brief as project_memory

WORK_VERBS = ("fix", "build", "add", "implement", "deploy", "investigate",
              "debug", "create", "refactor", "ship", "release", "update", "do:")

_MAX_SIGNALS = 5


@dataclass
class Decision:
    kind: str            # 'reply' | 'dispatch'
    reply_text: str = ""


def decide(cfg, event_row, *, conn=None, team_id=None) -> Decision:
    payload = event_row.get("payload") or {}
    # Test/scripted seam: the event payload may carry a pre-computed decision.
    scripted = payload.get("_front_result")
    if scripted:
        return Decision(kind=scripted.get("action", "reply"),
                        reply_text=scripted.get("reply", ""))
    # Production path: call the manager engine with assembled context here;
    # on any failure, fall through to the rule below.
    text = payload.get("text", "").strip().lower()
    if any(v in text for v in WORK_VERBS):
        return Decision(kind="dispatch")
    return Decision(kind="reply", reply_text="Got it.")


def manager_state(conn: psycopg.Connection, cfg, team_id: str) -> str:
    """Build the manager state block for context injection before a converse run.

    Includes: in-flight requests (open/awaiting_approval), PRs Argus opened
    (actions with type in open_pr/pr and provider_ref starting with http),
    recent signals (last 5), and a read-only gh usage note if a project repo
    is known."""
    brief = project_memory.build(conn, cfg, team_id, datetime.now(timezone.utc))
    lines = [project_memory.render_prompt(brief), "", "--- ARGUS LIVE STATE ---"]

    # Recent signals (last _MAX_SIGNALS).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source, payload FROM events "
            "WHERE team_id=%s AND kind='signal' "
            "ORDER BY received_at DESC LIMIT %s",
            (team_id, _MAX_SIGNALS))
        sigs = cur.fetchall()
    if sigs:
        lines.append("Recent signals:")
        for source, payload in sigs:
            summary = (payload or {}).get("title") or (payload or {}).get("msg") or ""
            lines.append(f"  [{source}] {summary}".rstrip())
    else:
        lines.append("Recent signals: none")

    # Open pull requests, PRE-FETCHED here in Python (which has network). The
    # manager engine runs in a sandbox WITHOUT network, so it cannot run gh
    # itself; we inject the data instead. This also keeps the manager tool-less
    # (read-only by construction), so it can never act, only answer or dispatch.
    owner_repo = _gh_owner_repo(cfg, team_id)
    prs = _open_prs(owner_repo) if owner_repo else []
    if prs:
        lines.append("Open pull requests:")
        for pr in prs:
            draft = " [draft]" if pr.get("isDraft") else ""
            url = pr.get("url", "")
            lines.append(f"  #{pr.get('number')} {pr.get('title', '')}{draft} {url}".rstrip())
            body = (pr.get("body") or "").strip().replace("\r", " ").replace("\n", " ")
            if body:
                lines.append(f"      about: {body[:400]}")
    elif owner_repo:
        lines.append("Open pull requests: none")

    # Friendly "no current work" note when there is truly nothing.
    if not brief.current_work and not brief.recent_outcomes and not sigs and not prs:
        lines.append("(no current work for this team)")

    lines.append("--- END ARGUS LIVE STATE ---")
    return "\n".join(lines)


def _open_prs(owner_repo: str) -> list:
    """Pre-fetch open PRs via read-only gh (Python has network; the manager
    sandbox does not). Returns a list of {number,title,isDraft}, or [] on any
    failure (gh missing, not authed, timeout) so context assembly never breaks."""
    import json
    try:
        r = subprocess.run(
            ["gh", "pr", "list", "-R", owner_repo, "--state", "open",
             "--json", "number,title,isDraft,url,body", "--limit", "20"],
            capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return []
        return json.loads(r.stdout or "[]")
    except Exception:
        return []


def _gh_owner_repo(cfg, team_id: str) -> Optional[str]:
    """Return owner/repo for the team's project, or None if not available.

    Prefers project.github_repo override; falls back to parsing the git remote."""
    try:
        team = cfg.team(team_id)
    except KeyError:
        return None
    project = getattr(team, "project", None)
    if project is None:
        return None
    # Explicit override takes priority.
    override = getattr(project, "github_repo", None)
    if override:
        return override
    # Derive from the origin remote of the project repo.
    repo = getattr(project, "repo", None)
    remote = getattr(project, "remote", "origin")
    if not repo:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", remote],
            capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return None
        url = result.stdout.strip()
        return _parse_owner_repo(url)
    except Exception:
        return None


def _parse_owner_repo(url: str) -> Optional[str]:
    """Parse owner/repo from a git remote URL (https or ssh)."""
    if not url:
        return None
    # SSH: git@github.com:owner/repo.git
    if url.startswith("git@"):
        rest = url.split(":", 1)[-1]
        return rest.rstrip("/").removesuffix(".git") if rest else None
    # HTTPS: https://github.com/owner/repo.git
    try:
        path = urlparse(url).path.lstrip("/")
        return path.rstrip("/").removesuffix(".git") if path else None
    except Exception:
        return None
