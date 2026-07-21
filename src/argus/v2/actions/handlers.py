"""Real outward-action handlers. Each splits a pure command-builder (gate-tested)
from execution via an injectable runner (subprocess by default; a fake in tests).
The actual git/gh/deploy processes are NOT run in the gate."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from hashlib import sha256
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import psycopg

from argus.v2.actions import mergeability
from argus.v2.ownership.github import inspect_pr
from argus.v2.ownership.policy import assess_pr

log = logging.getLogger(__name__)


def _default_runner(argv, cwd=None) -> str:  # pragma: no cover
    r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} failed: {r.stderr.strip()}")
    return r.stdout


_CONFLICT_TITLE_PREFIX = "[conflicts] "
_ARGUS_PR_SIGNATURE_PREFIX = "argus-pr-signature:"
_GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_CANONICAL_PR_NUMBER = re.compile(r"^[1-9][0-9]*$")
_REPOSITORY_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")


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


def _normalize_signature_part(value) -> str:
    if isinstance(value, list):
        value = " ".join(sorted(str(item) for item in value))
    text = " ".join(str(value or "").lower().split())
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _pr_signature(payload: dict, *, team_id: str | None) -> str:
    raw = "|".join([
        _normalize_signature_part(team_id or "unknown"),
        _normalize_signature_part(payload.get("title")),
        _normalize_signature_part(payload.get("request")),
        _normalize_signature_part(payload.get("summary_short")),
        _normalize_signature_part(payload.get("changed_files")),
    ])
    return sha256(raw.encode("utf-8")).hexdigest()[:16]


def _body_with_signature(body: str, signature: str) -> str:
    marker = f"<!-- {_ARGUS_PR_SIGNATURE_PREFIX}{signature} -->"
    body = body or ""
    if marker in body:
        return body
    return f"{body.rstrip()}\n\n{marker}" if body.strip() else marker


def _existing_open_pr(payload: dict, *, runner: Callable, cwd: str | None,
                      signature: str) -> dict | None:
    if not cwd:
        return None
    try:
        out = runner([
            "gh", "pr", "list",
            "--state", "open",
            "--limit", "100",
            "--json", "number,url,title,body,headRefName",
        ], cwd=cwd)
        rows = json.loads(out or "[]")
    except Exception as exc:
        log.warning("open_pr duplicate scan failed, proceeding unchecked: %s", exc)
        return None
    if not isinstance(rows, list):
        return None
    marker = f"{_ARGUS_PR_SIGNATURE_PREFIX}{signature}"
    branch = str(payload.get("branch") or "")
    for row in rows:
        if not isinstance(row, dict):
            continue
        if marker in str(row.get("body") or "") or (
            branch and str(row.get("headRefName") or "") == branch
        ):
            return row
    return None


def _comment_duplicate_pr(existing: dict, payload: dict, *, runner: Callable,
                          cwd: str | None, signature: str) -> None:
    number = existing.get("number")
    if not number:
        return
    body = (
        "Argus skipped opening a duplicate PR for the same work signature.\n\n"
        f"- Signature: `{signature}`\n"
        f"- Attempted branch: `{payload.get('branch') or 'unknown'}`"
    )
    try:
        runner(["gh", "pr", "comment", str(number), "--body", body], cwd=cwd)
    except Exception as exc:
        log.warning("open_pr duplicate comment failed for PR %s: %s", number, exc)


def _apply_conflict_prefix(title: str, body: str, check: mergeability.MergeCheck) -> tuple[str, str]:
    """Prefix the title and note the conflict in the body so the owner is not
    surprised on GitHub; used only when the mergeability check still finds
    conflicts after the one rebase attempt."""
    if title.startswith(_CONFLICT_TITLE_PREFIX):
        prefixed_title = title
    else:
        prefixed_title = f"{_CONFLICT_TITLE_PREFIX}{title}"
    note = (
        "## Merge conflict warning\n"
        f"This branch does not merge cleanly into the current base ({check.detail}). "
        "A rebase was attempted and did not resolve it automatically. Manual "
        "conflict resolution is needed before merging."
    )
    conflict_body = f"{note}\n\n{body}" if body else note
    return prefixed_title, conflict_body


def _canonical_pr_number(value) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str) and _CANONICAL_PR_NUMBER.fullmatch(value):
        return int(value)
    raise RuntimeError("ownership action requires a positive canonical PR number")


def _expected_head_sha(payload: dict) -> str:
    value = payload.get("expected_head_sha")
    if not isinstance(value, str) or not _GIT_OBJECT_ID.fullmatch(value):
        raise RuntimeError("ownership action requires a full expected_head_sha")
    return value


def _absolute_checkout(value, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RuntimeError(f"{label} must be an absolute checkout path")
    expanded = os.path.expanduser(value)
    if not os.path.isabs(expanded):
        raise RuntimeError(f"{label} must be an absolute checkout path")
    return os.path.realpath(expanded)


def _git_common_dir(runner: Callable, cwd: str, *, action_cwd: bool) -> str:
    try:
        raw = runner(
            [
                "git", "rev-parse", "--path-format=absolute",
                "--git-common-dir",
            ],
            cwd=cwd,
        )
    except Exception as exc:
        label = "ownership action cwd" if action_cwd else "configured project repo"
        raise RuntimeError(f"cannot verify {label}") from exc
    value = raw.strip() if isinstance(raw, str) else ""
    if not value or "\x00" in value or not os.path.isabs(value):
        label = "ownership action cwd" if action_cwd else "configured project repo"
        raise RuntimeError(f"cannot verify {label}")
    return os.path.realpath(value)


def _validated_action_cwd(team, payload: dict, runner: Callable) -> str:
    project_repo = _absolute_checkout(
        team.project.repo, label="configured project repo")
    action_cwd = _absolute_checkout(
        payload.get("cwd"), label="ownership action cwd")
    project_common = _git_common_dir(
        runner, project_repo, action_cwd=False)
    action_common = _git_common_dir(
        runner, action_cwd, action_cwd=True)
    if action_common != project_common:
        raise RuntimeError(
            "ownership action cwd is not the configured project checkout "
            "or one of its linked worktrees")
    return action_cwd


def _repository_identity(value: str) -> tuple[str, str, str] | None:
    text = value.strip() if isinstance(value, str) else ""
    if not text or "\x00" in text:
        return None
    host = ""
    path = ""
    if "://" in text:
        try:
            parsed = urlsplit(text)
            parsed.port
        except ValueError:
            return None
        if parsed.scheme.lower() not in {"https", "ssh", "git"} or not parsed.hostname:
            return None
        host = parsed.hostname.lower()
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        path = parsed.path.strip("/")
    elif re.fullmatch(r"[^@/:\s]+@[^/:\s]+:.+", text):
        _user, remainder = text.split("@", 1)
        host, path = remainder.split(":", 1)
        host = host.lower()
    else:
        parts = text.strip("/").split("/")
        if len(parts) == 2:
            host = "github.com"
            path = text
        elif len(parts) == 3:
            host, path = parts[0].lower(), "/".join(parts[1:])
        else:
            return None
    parts = path.strip("/").split("/")
    if len(parts) != 2:
        return None
    owner, repo = parts
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not all(_REPOSITORY_SEGMENT.fullmatch(part) for part in (owner, repo)):
        return None
    if not host or any(char.isspace() for char in host):
        return None
    return host.lower(), owner.lower(), repo.lower()


def _pr_repository_identity(url: str) -> tuple[str, str, str] | None:
    try:
        parsed = urlsplit(url)
        parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return None
    parts = parsed.path.split("/")
    if len(parts) != 5 or parts[3] != "pull" or not parts[4].isdigit():
        return None
    host = parsed.hostname.lower()
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return host, parts[1].lower(), parts[2].lower()


def _configured_repository(team, runner: Callable, project_repo: str):
    configured = team.project.github_repo
    if configured:
        identity = _repository_identity(configured)
    else:
        try:
            remote_url = runner(
                ["git", "remote", "get-url", team.project.remote],
                cwd=project_repo,
            )
        except Exception as exc:
            raise RuntimeError(
                "cannot resolve configured GitHub repository") from exc
        identity = _repository_identity(remote_url)
    if identity is None:
        raise RuntimeError("cannot resolve configured GitHub repository")
    return identity


def _inspect_owned_pr(payload: dict, *, runner: Callable, cfg, team_id: str):
    if cfg is None or not team_id:
        raise RuntimeError("ownership action requires team configuration")
    team = cfg.team(team_id)
    if not team.ownership.enabled:
        raise RuntimeError("blocked by ownership policy: team ownership is disabled")
    if team.project is None:
        raise RuntimeError(
            "blocked by ownership policy: team project configuration is missing")
    number = _canonical_pr_number(payload.get("pr"))
    expected_head_sha = _expected_head_sha(payload)
    cwd = _validated_action_cwd(team, payload, runner)
    expected_repo = _configured_repository(
        team, runner, _absolute_checkout(
            team.project.repo, label="configured project repo"))
    pr = inspect_pr(
        cwd=cwd,
        pr_ref=str(number),
        runner=runner,
    )
    if pr.number != number:
        raise RuntimeError("inspected PR number does not match requested PR number")
    if _pr_repository_identity(pr.url) != expected_repo:
        raise RuntimeError("PR is not in the configured GitHub repository")
    if pr.head_sha != expected_head_sha:
        raise RuntimeError("inspected PR head SHA does not match expected_head_sha")
    return team, pr, number, expected_head_sha, cwd


def _require_owned_pr_policy(team, pr) -> None:
    decision = assess_pr(team, pr)
    if not decision.allowed:
        raise RuntimeError(
            f"blocked by ownership policy: {decision.reason}")


def _ownership_provider_ref(action: str, pr, expected_head_sha: str) -> str:
    return f"{action}:{pr.url}@{expected_head_sha}"


def run(action_type: str, payload: dict, *, runner: Callable = _default_runner,
        cfg=None, team_id: str | None = None,
        conn: psycopg.Connection | None = None) -> str:
    """Execute an action; return its provider_ref (e.g. the PR URL)."""
    if action_type == "open_pr":
        cwd = payload.get("cwd")
        title = payload["title"]
        body = payload.get("body", "")
        base = payload["base"]
        remote = payload["remote"]
        signature = _pr_signature(payload, team_id=team_id)
        existing = _existing_open_pr(payload, runner=runner, cwd=cwd, signature=signature)
        if existing:
            _comment_duplicate_pr(existing, payload, runner=runner, cwd=cwd,
                                  signature=signature)
            return str(existing.get("url") or f"existing-pr:{existing.get('number')}")
        body = _body_with_signature(body, signature)
        if cwd:
            # Pre-propose mergeability check: fetch the current remote base and
            # see if the work branch merges cleanly, rebasing once if not. Never
            # blocks the PR: a conflict just gets flagged in the title/body so
            # the owner isn't surprised by a CONFLICTING PR on GitHub later.
            check = mergeability.check(cwd, base=base, remote=remote)
            log.info("mergeability check for %s onto %s/%s: mergeable=%s rebased=%s "
                     "conflict=%s (%s)", payload.get("branch"), remote, base,
                     check.mergeable, check.rebased, check.conflict, check.detail)
            if check.conflict:
                title, body = _apply_conflict_prefix(title, body, check)
        for cmd in build_open_pr(branch=payload["branch"], base=base,
                                 remote=remote, title=title, body=body,
                                 draft=bool(payload.get("draft"))):
            out = runner(cmd, cwd=cwd)
        return (out or "").strip()
    if action_type == "ready_pr":
        team, pr, _number, expected_head_sha, cwd = _inspect_owned_pr(
            payload, runner=runner, cfg=cfg, team_id=team_id)
        _require_owned_pr_policy(team, pr)
        provider_ref = _ownership_provider_ref(
            "ready", pr, expected_head_sha)
        if pr.draft is False:
            return provider_ref
        if pr.draft is not True:
            raise RuntimeError("ready_pr requires an open draft PR")
        runner(
            ["gh", "pr", "ready", str(pr.number)],
            cwd=cwd,
        )
        return provider_ref
    if action_type == "merge_pr":
        team, pr, _number, expected_head_sha, cwd = _inspect_owned_pr(
            payload, runner=runner, cfg=cfg, team_id=team_id)
        provider_ref = _ownership_provider_ref(
            "merged", pr, expected_head_sha)
        if pr.state == "MERGED":
            return provider_ref
        if pr.state == "CLOSED":
            raise RuntimeError("merge_pr target closed without merge")
        _require_owned_pr_policy(team, pr)
        if pr.draft is not False:
            raise RuntimeError("merge_pr requires a non-draft PR")
        runner(
            [
                "gh", "pr", "merge", str(pr.number),
                "--squash", "--delete-branch",
            ],
            cwd=cwd,
        )
        return provider_ref
    if action_type == "support_reply":
        if conn is None or cfg is None or not team_id:
            raise RuntimeError("support_reply requires database and team configuration")
        from argus.v2.ownership import support as ownership_support

        return ownership_support.run_reply_action(conn, cfg, team_id, payload)
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
    if action_type == "sync_pr":
        repo = payload["repo"]
        number = str(payload["number"])
        repo_data = json.loads(runner([
            "gh", "repo", "view", repo, "--json", "defaultBranchRef",
        ]))
        base = str((repo_data.get("defaultBranchRef") or {}).get("name") or "")
        if not base:
            raise RuntimeError("sync_pr could not resolve the current default branch")
        pr = json.loads(runner([
            "gh", "pr", "view", number, "-R", repo,
            "--json", "baseRefName,headRefName,state",
        ]))
        if pr.get("state") != "OPEN":
            raise RuntimeError("sync_pr requires an open PR")
        if pr.get("baseRefName") != base:
            raise RuntimeError(
                f"sync_pr requires the PR base to be the current default branch {base}")
        head = str(pr.get("headRefName") or "")
        if not head:
            raise RuntimeError("sync_pr could not resolve the PR head branch")
        drift = json.loads(runner([
            "gh", "api", f"repos/{repo}/compare/{base}...{head}",
        ]))
        report = {
            "ahead": int(drift.get("ahead_by") or 0),
            "base": base,
            "behind": int(drift.get("behind_by") or 0),
            "head": head,
            "status": str(drift.get("status") or "unknown"),
            "updated": False,
        }
        if report["behind"] > 0:
            runner(["gh", "pr", "update-branch", number, "-R", repo])
            report["updated"] = True
        return json.dumps(report, sort_keys=True)
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
    if action_type == "set_user_balance":
        return _run_set_user_balance(payload, cfg=cfg, team_id=team_id)
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
    for t in (cfg.teams if cfg else []):
        for s in t.sources:
            if s.name == name:
                return s
    return None


def _run_bug_writeback(payload: dict, *, cfg) -> str:
    """PATCH a supabase bug row's notes column with Argus's verdict. Opt-in:
    only runs when the source config sets writeback: true or respond: true.
    Read-modify-write so existing notes are kept. Column/table names are
    validated (no injection)."""
    src = _source_by_name(cfg, payload.get("source_name"))
    enabled = src is not None and any(
        (src.config or {}).get(f) for f in ("writeback", "respond"))
    if not enabled:
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


def _firebase_request(method: str, url: str, *, token: str,
                      json_body: dict | None = None) -> dict:  # pragma: no cover
    import httpx

    response = httpx.request(
        method,
        url,
        headers={"Authorization": f"Bearer {token}"},
        json=json_body,
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {}


def _firestore_balance(document: dict) -> int:
    value = ((document.get("fields") or {}).get("balance") or {})
    raw = value.get("integerValue", value.get("doubleValue"))
    if raw is None:
        raise RuntimeError("Firebase user balance field missing")
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Firebase user balance is not numeric") from exc


def _run_set_user_balance(payload: dict, *, cfg, team_id: str | None) -> str:
    source = _source_by_name(cfg, payload.get("source_name"))
    actions = (source.config or {}).get("account_actions") if source else []
    if not source or source.type != "firebase" or "set_user_balance" not in (actions or []):
        raise RuntimeError("Firebase balance action is not enabled")
    if source.team not in (None, team_id):
        raise RuntimeError("Firebase balance source is not scoped to this team")
    if not str(payload.get("approval_proof") or "").startswith("owner control event "):
        raise RuntimeError("owner approval proof missing")
    if not payload.get("support_context_id") or not payload.get("idempotency_key"):
        raise RuntimeError("support audit context missing")

    email = str(payload.get("email") or "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise RuntimeError("valid customer email required")
    raw_balance = payload.get("balance")
    if isinstance(raw_balance, bool) or not isinstance(raw_balance, (int, str)):
        raise RuntimeError("balance must be an integer")
    if isinstance(raw_balance, str) and not raw_balance.isdigit():
        raise RuntimeError("balance must be an integer")
    balance = int(raw_balance)
    if balance < 0 or balance > 1_000_000:
        raise RuntimeError("balance outside allowed range")

    from argus.v2.connectors.firebase import _auth_token
    from urllib.parse import quote

    config = source.config or {}
    project = str(config.get("project") or "").strip()
    if not project:
        raise RuntimeError("Firebase project missing")
    token = _auth_token(source)
    lookup_url = (
        "https://identitytoolkit.googleapis.com/v1/projects/"
        f"{quote(project, safe='')}/accounts:lookup"
    )
    lookup = _firebase_request("POST", lookup_url, token=token,
                               json_body={"email": [email]})
    users = lookup.get("users") or []
    if len(users) != 1 or not users[0].get("localId"):
        raise RuntimeError("Firebase Auth user not found for support email")
    uid = str(users[0]["localId"])
    document_url = (
        "https://firestore.googleapis.com/v1/projects/"
        f"{quote(project, safe='')}/databases/(default)/documents/users/"
        f"{quote(uid, safe='')}"
    )
    before = _firestore_balance(
        _firebase_request("GET", document_url, token=token))
    if before != balance:
        patch_url = (
            f"{document_url}?updateMask.fieldPaths=balance"
            "&currentDocument.exists=true"
        )
        _firebase_request(
            "PATCH",
            patch_url,
            token=token,
            json_body={"fields": {"balance": {"integerValue": str(balance)}}},
        )
    after = _firestore_balance(
        _firebase_request("GET", document_url, token=token))
    if after != balance:
        raise RuntimeError(
            f"Firebase balance verification failed: expected {balance}, got {after}")
    return json.dumps({
        "project": project,
        "uid": uid,
        "email": email,
        "before": before,
        "after": after,
        "idempotency_key": str(payload["idempotency_key"]),
    }, separators=(",", ":"), sort_keys=True)


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
    _require_live_readiness(payload)
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


_LIVE_READINESS_REQUIRED = (
    "approval_proof",
    "durable_media",
    "cta_route",
    "dm_activation",
    "metricool_target",
    "connector_auth",
)


def _require_live_readiness(payload: dict) -> None:
    proof = payload.get("live_readiness") or payload.get("readiness")
    if not isinstance(proof, dict):
        missing = ", ".join(_LIVE_READINESS_REQUIRED)
        raise RuntimeError(f"live readiness proof missing: {missing}")
    missing = [key for key in _LIVE_READINESS_REQUIRED if not str(proof.get(key) or "").strip()]
    if missing:
        raise RuntimeError(f"live readiness proof incomplete: {', '.join(missing)}")


def _required(payload: dict, key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise RuntimeError(f"{key} is required")
    return value


def _run_root() -> Path:
    return Path(os.environ.get("ARGUS_RUN_ROOT", str(Path.home() / "argus-run")))
