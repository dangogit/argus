"""Real outward-action handlers. Each splits a pure command-builder (gate-tested)
from execution via an injectable runner (subprocess by default; a fake in tests).
The actual git/gh/deploy processes are NOT run in the gate."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Callable


def _default_runner(argv, cwd=None) -> str:  # pragma: no cover
    r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} failed: {r.stderr.strip()}")
    return r.stdout


def build_open_pr(*, branch, base, remote, title, body, draft=False):
    create = [
        "gh", "pr", "create", "--base", base, "--head", branch,
        "--title", title, "--body", body,
    ]
    if draft:
        create.append("--draft")
    return [
        ["git", "push", remote, branch],
        create,
    ]


def run(action_type: str, payload: dict, *, runner: Callable = _default_runner,
        cfg=None, team_id: str | None = None) -> str:
    """Execute an action; return its provider_ref (e.g. the PR URL)."""
    if action_type == "open_pr":
        cwd = payload.get("cwd")
        for cmd in build_open_pr(branch=payload["branch"], base=payload["base"],
                                 remote=payload["remote"], title=payload["title"],
                                 body=payload.get("body", ""),
                                 draft=bool(payload.get("draft"))):
            out = runner(cmd, cwd=cwd)
        return (out or "").strip()
    if action_type == "merge_pr":
        return runner(["gh", "pr", "merge", payload["pr"], "--squash"], cwd=payload.get("cwd")).strip()
    if action_type == "deploy":
        return runner(["bash", "-lc", payload["command"]], cwd=payload.get("cwd")).strip()
    if action_type == "close_pr":
        # gh pr close <number> -R <owner/repo>
        out = runner(["gh", "pr", "close", str(payload["number"]),
                      "-R", payload["repo"]])
        return (out or f"closed:{payload['number']}").strip()
    if action_type == "comment_pr":
        # gh pr comment <number> -R <owner/repo> --body <text>
        out = runner(["gh", "pr", "comment", str(payload["number"]),
                      "-R", payload["repo"],
                      "--body", payload.get("body", "")])
        return (out or f"commented:{payload['number']}").strip()
    if action_type == "reopen_pr":
        # gh pr reopen <number> -R <owner/repo>
        out = runner(["gh", "pr", "reopen", str(payload["number"]),
                      "-R", payload["repo"]])
        return (out or f"reopened:{payload['number']}").strip()
    if action_type.startswith("calendar_"):
        return _calendar(action_type.removeprefix("calendar_"), payload, runner)
    if action_type.startswith("email_"):
        return _email(action_type.removeprefix("email_"), payload, cfg, team_id)
    if action_type == "content_queue":
        return _content_queue(payload)
    if action_type == "social_publish":
        return _social_publish(payload, runner)
    if action_type == "bug_writeback":
        return _run_bug_writeback(payload, cfg=cfg)
    raise KeyError(action_type)


_SB_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def compose_writeback_notes(current: str | None, note: str) -> str:
    """Append Argus's verdict to a bug row's existing notes (newline-separated),
    so prior notes (e.g. the v1 HQ analysis) are preserved."""
    current = (current or "").strip()
    note = (note or "").strip()
    return f"{current}\n\n{note}" if current else note


def _sb_headers(key: str) -> dict:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _sb_get(url: str, headers: dict) -> list:  # pragma: no cover
    import httpx
    r = httpx.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def _sb_patch(url: str, headers: dict, body: dict) -> list:  # pragma: no cover
    import httpx
    r = httpx.patch(url, headers=headers, json=body, timeout=15)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def _source_by_name(cfg, name):
    for s in (cfg.company.sources if cfg and cfg.company else []):
        if s.name == name:
            return s
    return None


def _run_bug_writeback(payload: dict, *, cfg) -> str:
    """PATCH a supabase bug row's notes column with Argus's verdict. Opt-in:
    only runs when the source config sets writeback: true. Read-modify-write so
    existing notes are kept. Column/table names are validated (no injection)."""
    src = _source_by_name(cfg, payload.get("source_name"))
    if src is None or not (src.config or {}).get("writeback"):
        return "skipped: writeback not enabled"
    cfgd = src.config or {}
    base = (cfgd.get("url") or "").rstrip("/")
    key = src.secret or ""
    table = cfgd.get("table", "bug_reports")
    id_col = cfgd.get("id_column", "id")
    notes_col = cfgd.get("notes_column", "admin_notes")
    if not (_SB_NAME.match(table) and _SB_NAME.match(id_col) and _SB_NAME.match(notes_col)):
        return "skipped: unsafe table/column name"
    if not base or not key:
        return "skipped: source url/key missing"
    from urllib.parse import quote
    row_id = quote(str(payload["row_id"]), safe="")
    rows = _sb_get(f"{base}/rest/v1/{table}?{id_col}=eq.{row_id}&select={notes_col}",
                   _sb_headers(key))
    current = (rows[0].get(notes_col) if rows else "") or ""
    body = {notes_col: compose_writeback_notes(current, payload.get("note", ""))}
    status = payload.get("status") or cfgd.get("writeback_status")
    if status:
        status_col = cfgd.get("status_column", "status")
        if _SB_NAME.match(status_col):
            body[status_col] = status
    _sb_patch(f"{base}/rest/v1/{table}?{id_col}=eq.{row_id}", _sb_headers(key), body)
    return f"writeback:{table}:{payload['row_id']}"


def _calendar(verb: str, payload: dict, runner: Callable) -> str:
    allowed = {"ping", "list", "get", "create", "update", "delete"}
    if verb not in allowed:
        raise KeyError(f"calendar_{verb}")
    from argus.v2 import calendar

    return calendar.run(verb, payload, json_output=bool(payload.get("json", True)))


def _email(verb: str, payload: dict, cfg, team_id: str | None) -> str:
    transport = _email_transport(cfg, team_id)
    if verb == "list":
        items = transport.list_unread(int(payload.get("limit") or 10))
        return json.dumps([item.__dict__ for item in items], separators=(",", ":"))
    if verb == "search":
        query = str(payload.get("query") or payload.get("q") or payload.get("text") or "").strip()
        if not query:
            raise RuntimeError("query is required")
        items = transport.search(query, int(payload.get("limit") or 10))
        return json.dumps([item.__dict__ for item in items], separators=(",", ":"))
    if verb == "read":
        return transport.read(_required(payload, "thread_id"))
    if verb == "reply":
        transport.reply(_required(payload, "thread_id"), _required(payload, "body"))
        return f"email:reply:{payload['thread_id']}"
    if verb == "archive":
        transport.archive(_required(payload, "thread_id"))
        return f"email:archive:{payload['thread_id']}"
    if verb == "draft":
        draft_dir = _run_root() / "personal" / "email-drafts"
        draft_dir.mkdir(parents=True, exist_ok=True)
        draft_id = str(abs(hash(json.dumps(payload, sort_keys=True))))[:12]
        (draft_dir / f"{draft_id}.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return f"email:draft:{draft_id}"
    raise KeyError(f"email_{verb}")


def _email_transport(cfg, team_id: str | None):
    from argus.v2.support.apps_script import AppsScriptTransport

    source = _team_source(cfg, team_id, {"gmail_apps_script", "email_apps_script",
                                        "support_apps_script"})
    if source:
        url = source.config.get("url")
        key = source.secret or source.config.get("key")
    else:
        if cfg is not None and team_id and team_id != "personal":
            raise RuntimeError(f"team email transport not configured for {team_id}")
        url = os.environ.get("ARGUS_PERSONAL_GMAIL_APPS_SCRIPT_URL")
        key = os.environ.get("ARGUS_PERSONAL_GMAIL_APPS_SCRIPT_KEY")
    if not url or not key:
        raise RuntimeError("personal email transport not configured")
    return AppsScriptTransport(url=url, key=key)


def _team_source(cfg, team_id: str | None, types: set[str]):
    if cfg is None or not team_id:
        return None
    try:
        team = cfg.team(team_id)
    except KeyError:
        return None
    for source in list(team.sources) + list(cfg.company.sources):
        if source.type in types and (source.scope == "team" or source.team in (None, team_id)):
            return source
    return None


def _content_queue(payload: dict) -> str:
    from argus.v2.content import state

    queue_id = state.queue_add(
        _required(payload, "project"),
        _required(payload, "platform"),
        _required(payload, "brief"),
    )
    return f"content:queue:{queue_id}"


def _social_publish(payload: dict, runner: Callable) -> str:
    if os.environ.get("ARGUS_CONTENT_PUBLISH_ENABLED") != "1":
        raise RuntimeError("social publishing not configured")
    command = os.environ.get("ARGUS_SOCIAL_PUBLISH_COMMAND")
    if not command:
        raise RuntimeError("social publishing command not configured")
    previous = {}
    for key, value in payload.items():
        env_key = f"ARGUS_SOCIAL_{key.upper()}"
        previous[env_key] = os.environ.get(env_key)
        os.environ[env_key] = str(value)
    try:
        return runner(["bash", "-lc", command], cwd=None).strip()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _required(payload: dict, key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise RuntimeError(f"{key} is required")
    return value


def _run_root() -> Path:
    return Path(os.environ.get("ARGUS_RUN_ROOT", str(Path.home() / "argus-run")))
