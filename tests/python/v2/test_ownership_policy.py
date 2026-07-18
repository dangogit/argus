import json
from dataclasses import FrozenInstanceError

import pytest

from argus.v2.config.schema import (
    MANDATORY_OWNERSHIP_BLOCKED_GLOBS,
    OwnershipCodePolicy,
    OwnershipPolicy,
    Pipeline,
    Project,
    Role,
    Team,
)
from argus.v2.ownership.github import PullRequestState
from argus.v2.ownership import policy

SHA40 = "a" * 40


def _team(*, enabled=True, bases=None, required_checks=None, blocked_globs=None,
          prefix="argus", auto_merge=True):
    code = OwnershipCodePolicy(
        auto_merge=auto_merge,
        allowed_base_branches=bases or ["staging"],
        required_checks=required_checks or ["test"],
        blocked_globs=(
            list(MANDATORY_OWNERSHIP_BLOCKED_GLOBS)
            if blocked_globs is None else blocked_globs
        ),
    )
    return Team(
        name="dev",
        roles=[Role(name="builder", kind="builder", prompt="build")],
        pipeline=Pipeline(stages=["builder"]),
        project=Project(repo="/repo", work_branch_prefix=prefix),
        ownership=OwnershipPolicy(enabled=enabled, code=code),
    )


def _pr(*, number=42, url="https://github.com/acme/luma/pull/42",
        state="OPEN", draft=True, clean=True, base="staging",
        head="argus/req-1", head_sha=SHA40, files=None, checks=None,
        checks_passed=True):
    return PullRequestState(
        number=number,
        url=url,
        state=state,
        draft=draft,
        clean=clean,
        base=base,
        head=head,
        head_sha=head_sha,
        changed_files=tuple(files if files is not None else ["src/App.tsx"]),
        checks=tuple(checks if checks is not None else ["test"]),
        checks_passed=checks_passed,
    )


@pytest.mark.parametrize("path", [
    ".github/workflows/deploy.yml",
    ".env",
    "api-secret.txt",
    "auth/session.ts",
    "migrations/0031.sql",
    "supabase/migrations/0031.sql",
    "src/auth/session.ts",
    "src/billing/checkout.ts",
    "package-lock.json",
])
def test_sensitive_paths_block_auto_merge(path):
    decision = policy.assess_pr(_team(), _pr(files=[path]))

    assert decision.allowed is False
    assert path in decision.reason


def test_hard_blocked_globs_reuse_schema_safety_floor():
    assert policy.HARD_BLOCKED_GLOBS is MANDATORY_OWNERSHIP_BLOCKED_GLOBS


def test_configured_blocked_path_adds_to_hard_safety_floor():
    team = _team(blocked_globs=["docs/private/**"])

    decision = policy.assess_pr(team, _pr(files=["docs/private/runbook.md"]))

    assert decision.allowed is False
    assert "docs/private/runbook.md" in decision.reason


def test_only_argus_clean_checked_staging_pr_is_allowed():
    decision = policy.assess_pr(
        _team(),
        _pr(base="staging", head="argus/req-1", clean=True,
            checks_passed=True, files=["src/App.tsx"]),
    )

    assert decision.allowed is True
    assert decision.reason == "PR satisfies low-risk ownership policy"


def test_safe_draft_is_allowed_for_ready_assessment():
    assert policy.assess_pr(_team(), _pr(draft=True)).allowed is True


@pytest.mark.parametrize(("change", "reason"), [
    ({"base": "develop"}, "base branch"),
    ({"head": "human/fix"}, "branch prefix"),
    ({"clean": False}, "merge state"),
    ({"files": []}, "changed-file"),
    ({"checks": []}, "required checks"),
    ({"checks_passed": False}, "checks are not successful"),
    ({"state": "CLOSED"}, "not open"),
    ({"state": "MERGED"}, "not open"),
])
def test_policy_blocks_unsafe_pr_state(change, reason):
    decision = policy.assess_pr(_team(), _pr(**change))

    assert decision.allowed is False
    assert reason in decision.reason


def test_policy_blocks_when_one_required_check_is_missing():
    team = _team(required_checks=["test", "build"])

    decision = policy.assess_pr(team, _pr(checks=["test"]))

    assert decision.allowed is False
    assert "build" in decision.reason


def test_policy_blocks_production_base_even_when_config_lists_it():
    team = _team(bases=["main"], auto_merge=False)

    decision = policy.assess_pr(team, _pr(base="main"))

    assert decision.allowed is False
    assert "production" in decision.reason


def test_policy_blocks_disabled_ownership_and_missing_project():
    disabled = policy.assess_pr(_team(enabled=False), _pr())
    no_project = _team().model_copy(update={"project": None})

    assert disabled.allowed is False
    assert "disabled" in disabled.reason
    assert policy.assess_pr(no_project, _pr()).allowed is False


def test_policy_blocks_unknown_required_pr_fields():
    decision = policy.assess_pr(_team(), PullRequestState())

    assert decision.allowed is False
    assert "incomplete" in decision.reason


def test_policy_decision_contains_machine_readable_evidence():
    decision = policy.assess_pr(_team(), _pr())

    assert decision.evidence == {
        "pr": 42,
        "base": "staging",
        "head": "argus/req-1",
        "head_sha": SHA40,
        "changed_files": ("src/App.tsx",),
        "checks": ("test",),
    }

    assert json.loads(json.dumps(decision.evidence_dict())) == {
        "pr": 42,
        "base": "staging",
        "head": "argus/req-1",
        "head_sha": SHA40,
        "changed_files": ["src/App.tsx"],
        "checks": ["test"],
    }


def test_policy_evidence_is_deeply_immutable():
    evidence = policy.assess_pr(_team(), _pr()).evidence

    with pytest.raises(TypeError):
        evidence["pr"] = 7
    with pytest.raises(TypeError):
        evidence["changed_files"][0] = "src/Other.tsx"
    with pytest.raises(AttributeError):
        evidence["checks"].append("build")


def test_pull_request_state_normalizes_surrounding_whitespace_before_policy():
    pr = _pr(
        url="  https://github.com/acme/luma/pull/42  ",
        base="  staging  ",
        head="  argus/req-1  ",
        head_sha=f"  {SHA40}  ",
        files=["  src/App.tsx  "],
        checks=["  test  "],
    )

    decision = policy.assess_pr(_team(), pr)

    assert decision.allowed is True
    assert decision.evidence["changed_files"] == ("src/App.tsx",)


@pytest.mark.parametrize("change", [
    {"url": "   "},
    {"url": "http://github.com/acme/luma/pull/42"},
    {"url": "https://github.com/acme/luma/pull/7"},
    {"head_sha": "   "},
    {"head_sha": "not-a-hex-sha"},
    {"base": "   "},
    {"head": "   "},
    {"head": "argus/../req"},
    {"head": "argus/bad name"},
    {"head": "argus/foo.lock/req"},
    {"files": ["   "]},
    {"files": ["/etc/passwd"]},
    {"files": ["src/../secret.txt"]},
    {"files": ["src/\x00App.tsx"]},
    {"checks": ["   "]},
    {"checks": ["test\x00name"]},
])
def test_policy_denies_malformed_manually_constructed_pr_state(change):
    decision = policy.assess_pr(_team(), _pr(**change))

    assert decision.allowed is False


def test_github_and_policy_dataclasses_are_frozen():
    pr = _pr()
    decision = policy.assess_pr(_team(), pr)

    with pytest.raises(FrozenInstanceError):
        pr.state = "MERGED"
    with pytest.raises(FrozenInstanceError):
        decision.allowed = False
