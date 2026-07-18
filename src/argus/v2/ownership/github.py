"""Deterministic, read-only GitHub state inspection for ownership work."""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Callable


Runner = Callable[..., str]

_PR_FIELDS = (
    "number,url,state,isDraft,mergeStateStatus,baseRefName,headRefName,"
    "headRefOid,mergeCommit,files,statusCheckRollup"
)
_DEPLOY_FIELDS = "databaseId,status,conclusion,url,headSha"
_PR_URL = re.compile(
    r"^https://github\.com/(?P<repo>[^/]+/[^/]+)/pull/(?P<number>\d+)/?$"
)


@dataclass(frozen=True)
class PullRequestState:
    number: int = 0
    url: str = ""
    state: str = "UNKNOWN"
    draft: bool | None = None
    clean: bool = False
    base: str = ""
    head: str = ""
    head_sha: str = ""
    merge_sha: str = ""
    changed_files: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    checks_passed: bool = False


@dataclass(frozen=True)
class DeployState:
    workflow: str
    commit_sha: str
    run_id: int | None = None
    status: str = "UNKNOWN"
    conclusion: str = "UNKNOWN"
    url: str = ""
    head_sha: str = ""

    @property
    def found(self) -> bool:
        return (
            isinstance(self.run_id, int)
            and not isinstance(self.run_id, bool)
            and self.run_id > 0
            and bool(self.url)
            and self.head_sha == self.commit_sha
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


def _default_runner(argv: list[str], cwd=None) -> str:  # pragma: no cover
    proc = subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"{argv[0]} failed")
    return proc.stdout


def _string(value) -> str:
    return value if isinstance(value, str) and value else ""


def _parse_files(value) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        return ()
    paths: list[str] = []
    for row in value:
        if not isinstance(row, dict) or not _string(row.get("path")):
            return ()
        paths.append(row["path"])
    return tuple(paths)


def _check_result(row: dict) -> tuple[str, bool]:
    name = _string(row.get("name")) or _string(row.get("context"))
    if not name:
        return "", False

    status = _string(row.get("status")).upper()
    conclusion = _string(row.get("conclusion")).upper()
    if status or conclusion:
        return name, status == "COMPLETED" and conclusion in {"SUCCESS", "SKIPPED"}

    state = _string(row.get("state")).upper()
    return name, state in {"SUCCESS", "SKIPPED"}


def _parse_checks(value) -> tuple[tuple[str, ...], bool]:
    if not isinstance(value, list) or not value:
        return (), False
    names: list[str] = []
    passed = True
    for row in value:
        if not isinstance(row, dict):
            return (), False
        name, safe = _check_result(row)
        if not name:
            return (), False
        names.append(name)
        passed = passed and safe
    return tuple(names), passed


def _parse_pr(raw) -> PullRequestState:
    if not isinstance(raw, dict):
        return PullRequestState()

    number = raw.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        number = 0
    state = _string(raw.get("state")).upper()
    if state not in {"OPEN", "CLOSED", "MERGED"}:
        state = "UNKNOWN"
    draft = raw.get("isDraft")
    if not isinstance(draft, bool):
        draft = None
    merge_state = _string(raw.get("mergeStateStatus")).upper()
    merge_commit = raw.get("mergeCommit")
    merge_sha = (
        _string(merge_commit.get("oid"))
        if isinstance(merge_commit, dict) else ""
    )
    checks, checks_passed = _parse_checks(raw.get("statusCheckRollup"))
    return PullRequestState(
        number=number,
        url=_string(raw.get("url")),
        state=state,
        draft=draft,
        clean=merge_state == "CLEAN",
        base=_string(raw.get("baseRefName")),
        head=_string(raw.get("headRefName")),
        head_sha=_string(raw.get("headRefOid")),
        merge_sha=merge_sha,
        changed_files=_parse_files(raw.get("files")),
        checks=checks,
        checks_passed=checks_passed,
    )


def inspect_pr(*, cwd, pr_ref, runner: Runner | None = None) -> PullRequestState:
    """Read exactly the PR fields needed by the ownership policy."""
    runner = runner or _default_runner
    ref = str(pr_ref)
    argv = ["gh", "pr", "view", ref]
    if cwd is None:
        match = _PR_URL.fullmatch(ref)
        if match is None:
            return PullRequestState()
        ref = match.group("number")
        argv = [
            "gh", "pr", "view", ref, "--repo", match.group("repo")]
    argv += ["--json", _PR_FIELDS]
    try:
        raw = json.loads(runner(argv, cwd=cwd) or "")
    except (json.JSONDecodeError, TypeError):
        return PullRequestState()
    return _parse_pr(raw)


def inspect_deploy(*, cwd, workflow, commit_sha,
                   runner: Runner | None = None) -> DeployState:
    """Read workflow runs for one exact merge commit."""
    runner = runner or _default_runner
    workflow = str(workflow)
    commit_sha = str(commit_sha)
    unknown = DeployState(workflow=workflow, commit_sha=commit_sha)
    if not cwd or not workflow or not commit_sha:
        return unknown
    argv = [
        "gh", "run", "list", "--workflow", workflow,
        "--commit", commit_sha, "--json", _DEPLOY_FIELDS, "--limit", "10",
    ]
    try:
        rows = json.loads(runner(argv, cwd=cwd) or "")
    except (json.JSONDecodeError, TypeError):
        return unknown
    if not isinstance(rows, list):
        return unknown
    for row in rows:
        if not isinstance(row, dict) or _string(row.get("headSha")) != commit_sha:
            continue
        run_id = row.get("databaseId")
        if (
            not isinstance(run_id, int)
            or isinstance(run_id, bool)
            or run_id <= 0
        ):
            return unknown
        return DeployState(
            workflow=workflow,
            commit_sha=commit_sha,
            run_id=run_id,
            status=_string(row.get("status")).upper() or "UNKNOWN",
            conclusion=_string(row.get("conclusion")).upper() or "UNKNOWN",
            url=_string(row.get("url")),
            head_sha=_string(row.get("headSha")),
        )
    return unknown
