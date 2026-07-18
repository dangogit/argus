"""Pure low-risk policy assessment for Argus-owned pull requests."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType

from argus.v2.config.schema import (
    MANDATORY_OWNERSHIP_BLOCKED_GLOBS,
    PROTECTED_AUTO_MERGE_BRANCHES,
)
from argus.v2.ownership.github import PullRequestState


HARD_BLOCKED_GLOBS = MANDATORY_OWNERSHIP_BLOCKED_GLOBS
EvidenceValue = int | str | tuple[str, ...]


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    evidence: Mapping[str, EvidenceValue]

    def __post_init__(self) -> None:
        frozen = {
            str(key): tuple(value) if isinstance(value, (list, tuple)) else value
            for key, value in self.evidence.items()
        }
        object.__setattr__(self, "evidence", MappingProxyType(frozen))

    def evidence_dict(self) -> dict[str, int | str | list[str]]:
        """Return a new JSON and database-safe copy of immutable evidence."""
        return {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in self.evidence.items()
        }


def _evidence(pr: PullRequestState) -> Mapping[str, EvidenceValue]:
    return MappingProxyType({
        "pr": pr.number,
        "base": pr.base,
        "head": pr.head,
        "head_sha": pr.head_sha,
        "changed_files": pr.changed_files,
        "checks": pr.checks,
    })


def _decision(pr: PullRequestState, allowed: bool, reason: str) -> PolicyDecision:
    return PolicyDecision(
        allowed=allowed,
        reason=reason,
        evidence=_evidence(pr),
    )


def _blocked_path(team, path: str) -> bool:
    configured = tuple(team.ownership.code.blocked_globs)
    globs = tuple(dict.fromkeys((*HARD_BLOCKED_GLOBS, *configured)))
    candidate = PurePosixPath(path)
    candidates = (candidate, *candidate.parents)
    for pattern in globs:
        variants = (pattern, pattern[3:]) if pattern.startswith("**/") else (pattern,)
        if any(item.match(variant) for variant in variants for item in candidates):
            return True
    return False


def assess_pr(team, pr: PullRequestState) -> PolicyDecision:
    """Fail closed unless the PR meets every deterministic low-risk rule."""
    if not team.ownership.enabled:
        return _decision(pr, False, "team ownership is disabled")
    if team.project is None:
        return _decision(pr, False, "team project configuration is missing")
    if (
        not isinstance(pr.number, int)
        or isinstance(pr.number, bool)
        or pr.number <= 0
        or not pr.url
        or pr.state == "UNKNOWN"
        or pr.draft is None
        or not pr.base
        or not pr.head
        or not pr.head_sha
    ):
        return _decision(pr, False, "GitHub PR inspection is incomplete")
    if pr.state != "OPEN":
        return _decision(pr, False, f"PR is not open: {pr.state}")

    base = pr.base.strip()
    if base.lower() in PROTECTED_AUTO_MERGE_BRANCHES:
        return _decision(
            pr, False, f"production base branch is blocked: {pr.base}")
    allowed_bases = {branch.strip() for branch in
                     team.ownership.code.allowed_base_branches if branch.strip()}
    if base not in allowed_bases:
        return _decision(pr, False, f"base branch is not allowlisted: {pr.base}")

    prefix = team.project.work_branch_prefix.strip().rstrip("/")
    if not prefix or not (pr.head == prefix or pr.head.startswith(f"{prefix}/")):
        return _decision(
            pr, False, f"PR does not use configured branch prefix: {pr.head}")
    if pr.clean is not True:
        return _decision(pr, False, "PR merge state is not clean")
    if not pr.changed_files:
        return _decision(pr, False, "PR has no changed-file evidence")
    for path in pr.changed_files:
        if _blocked_path(team, path):
            return _decision(pr, False, f"blocked changed path: {path}")

    required = {
        name.strip() for name in team.ownership.code.required_checks
        if name.strip()
    }
    if not pr.checks or any(not name for name in pr.checks):
        return _decision(
            pr, False, "required checks have incomplete check-name evidence")
    present = set(pr.checks)
    missing = sorted(required - present)
    if not required:
        return _decision(pr, False, "required checks are not configured")
    if missing:
        return _decision(
            pr, False, f"required checks are missing: {', '.join(missing)}")
    if pr.checks_passed is not True:
        return _decision(pr, False, "GitHub checks are not successful")
    return _decision(pr, True, "PR satisfies low-risk ownership policy")
