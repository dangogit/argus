"""Deterministic, read-only Vercel deployment inspection for ownership work."""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass

from argus.v2.ownership.github import (
    Runner,
    _https_url,
    _object_id,
    _string,
    normalize_branch_name,
)


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_PENDING_STATES = frozenset({"BUILDING", "INITIALIZING", "QUEUED"})
_FAILED_STATES = frozenset({"CANCELED", "CANCELLED", "ERROR"})


@dataclass(frozen=True)
class DeployState:
    project: str
    scope: str
    commit_sha: str
    expected_branch: str
    url: str = ""
    state: str = "UNKNOWN"
    head_sha: str = ""
    branch: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "project", _identifier(self.project))
        object.__setattr__(self, "scope", _identifier(self.scope))
        object.__setattr__(self, "commit_sha", _object_id(self.commit_sha))
        object.__setattr__(
            self, "expected_branch", normalize_branch_name(self.expected_branch))
        object.__setattr__(self, "url", _https_url(self.url))
        object.__setattr__(self, "state", _string(self.state).upper() or "UNKNOWN")
        object.__setattr__(self, "head_sha", _object_id(self.head_sha))
        object.__setattr__(self, "branch", normalize_branch_name(self.branch))

    @property
    def deployment_ref(self) -> str | None:
        return self.url or None

    @property
    def status(self) -> str:
        if self.state == "READY" or self.state in _FAILED_STATES:
            return "COMPLETED"
        if self.state in _PENDING_STATES:
            return "IN_PROGRESS"
        return "UNKNOWN"

    @property
    def conclusion(self) -> str:
        if self.state == "READY":
            return "SUCCESS"
        if self.state in _FAILED_STATES:
            return "FAILURE"
        return "UNKNOWN"

    @property
    def found(self) -> bool:
        return bool(
            self.project
            and self.scope
            and self.url
            and self.head_sha == self.commit_sha
            and self.branch == self.expected_branch
        )

    @property
    def completed(self) -> bool:
        return self.found and self.status == "COMPLETED"

    @property
    def successful(self) -> bool:
        return self.completed and self.conclusion == "SUCCESS"

    @property
    def failed(self) -> bool:
        return self.completed and self.conclusion != "SUCCESS"


def _identifier(value) -> str:
    text = _string(value)
    return text if _IDENTIFIER.fullmatch(text) else ""


def _default_runner(argv: list[str], cwd=None, *, auth_mode="environment") -> str:
    env = os.environ.copy()
    if auth_mode == "cli":
        env.pop("VERCEL_TOKEN", None)
    proc = subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, timeout=30, env=env)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"{argv[0]} failed")
    return proc.stdout


def inspect_deploy(*, cwd, project, scope, commit_sha, expected_branch, auth_mode,
                   runner: Runner | None = None) -> DeployState:
    """Read a Vercel deployment for one exact Git commit and project scope."""
    project = _identifier(project)
    scope = _identifier(scope)
    commit_sha = _object_id(commit_sha)
    expected_branch = normalize_branch_name(expected_branch)
    auth_mode = auth_mode if auth_mode in {"cli", "environment"} else ""
    unknown = DeployState(
        project=project,
        scope=scope,
        commit_sha=commit_sha,
        expected_branch=expected_branch,
    )
    if (
        not cwd
        or not project
        or not scope
        or not commit_sha
        or not expected_branch
        or not auth_mode
    ):
        return unknown
    argv = [
        "vercel", "list", project,
        "--scope", scope,
        "--meta", f"githubCommitSha={commit_sha}",
        "--meta", f"githubCommitRef={expected_branch}",
        "--format", "json", "--yes",
    ]
    try:
        if runner is None:
            response = _default_runner(argv, cwd=cwd, auth_mode=auth_mode)
        else:
            response = runner(argv, cwd=cwd)
        payload = json.loads(response or "")
    except (json.JSONDecodeError, TypeError):
        return unknown
    if not isinstance(payload, dict) or _string(payload.get("contextName")) != scope:
        return unknown
    rows = payload.get("deployments")
    if not isinstance(rows, list):
        return unknown
    for row in rows:
        if not isinstance(row, dict) or _string(row.get("name")) != project:
            continue
        meta = row.get("meta")
        if not isinstance(meta, dict):
            continue
        head_sha = _object_id(meta.get("githubCommitSha"))
        if head_sha != commit_sha:
            continue
        raw_url = _string(row.get("url"))
        url = raw_url if "://" in raw_url else f"https://{raw_url}"
        return DeployState(
            project=project,
            scope=scope,
            commit_sha=commit_sha,
            expected_branch=expected_branch,
            url=url,
            state=row.get("state"),
            head_sha=head_sha,
            branch=meta.get("githubCommitRef"),
        )
    return unknown
