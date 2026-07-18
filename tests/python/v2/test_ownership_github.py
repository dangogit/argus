import json

from argus.v2.ownership import github


PR_FIELDS = (
    "number,url,state,isDraft,mergeStateStatus,baseRefName,headRefName,"
    "headRefOid,mergeCommit,files,statusCheckRollup"
)


class FakeRunner:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, argv, cwd=None):
        self.calls.append((argv, cwd))
        return self.response


def _pr_payload(**overrides):
    payload = {
        "number": 42,
        "url": "https://github.com/acme/luma/pull/42",
        "state": "OPEN",
        "isDraft": True,
        "mergeStateStatus": "CLEAN",
        "baseRefName": "staging",
        "headRefName": "argus/req-1",
        "headRefOid": "abc123",
        "mergeCommit": None,
        "files": [{"path": "src/App.tsx"}],
        "statusCheckRollup": [{
            "name": "test",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
        }],
    }
    payload.update(overrides)
    return payload


def test_inspect_pr_parses_checks_and_files_with_exact_local_request():
    runner = FakeRunner(json.dumps(_pr_payload()))

    pr = github.inspect_pr(cwd="/repo", pr_ref="42", runner=runner)

    assert runner.calls == [
        (["gh", "pr", "view", "42", "--json", PR_FIELDS], "/repo"),
    ]
    assert pr.number == 42
    assert pr.state == "OPEN"
    assert pr.draft is True
    assert pr.clean is True
    assert pr.checks_passed is True
    assert pr.changed_files == ("src/App.tsx",)
    assert pr.checks == ("test",)


def test_inspect_pr_uses_repo_only_without_local_checkout():
    runner = FakeRunner(json.dumps(_pr_payload()))

    github.inspect_pr(
        cwd=None,
        pr_ref="https://github.com/acme/luma/pull/42",
        runner=runner,
    )

    assert runner.calls == [
        ([
            "gh", "pr", "view", "42", "--repo", "acme/luma",
            "--json", PR_FIELDS,
        ], None),
    ]


def test_inspect_pr_reads_merge_commit_for_merged_pr():
    runner = FakeRunner(json.dumps(_pr_payload(
        state="MERGED",
        mergeStateStatus="UNKNOWN",
        mergeCommit={"oid": "merge123"},
    )))

    pr = github.inspect_pr(cwd="/repo", pr_ref="42", runner=runner)

    assert pr.state == "MERGED"
    assert pr.merge_sha == "merge123"
    assert pr.clean is False


def test_inspect_pr_treats_missing_or_unknown_fields_as_unsafe():
    runner = FakeRunner(json.dumps({
        "number": 42,
        "url": "https://github.com/acme/luma/pull/42",
        "files": [{"path": "src/App.tsx"}, {}],
        "statusCheckRollup": [{"name": "test", "status": "QUEUED"}],
    }))

    pr = github.inspect_pr(cwd="/repo", pr_ref="42", runner=runner)

    assert pr.state == "UNKNOWN"
    assert pr.draft is None
    assert pr.clean is False
    assert pr.base == ""
    assert pr.head == ""
    assert pr.head_sha == ""
    assert pr.changed_files == ()
    assert pr.checks_passed is False


def test_inspect_pr_requires_every_rollup_entry_to_finish_safely():
    runner = FakeRunner(json.dumps(_pr_payload(statusCheckRollup=[
        {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"name": "lint", "status": "COMPLETED", "conclusion": "SKIPPED"},
        {"name": "build", "status": "IN_PROGRESS", "conclusion": None},
    ])))

    pr = github.inspect_pr(cwd="/repo", pr_ref="42", runner=runner)

    assert pr.checks == ("test", "lint", "build")
    assert pr.checks_passed is False


def test_inspect_deploy_parses_matching_run_with_exact_request():
    runner = FakeRunner(json.dumps([
        {
            "databaseId": 99,
            "status": "completed",
            "conclusion": "success",
            "url": "https://github.com/acme/luma/actions/runs/99",
            "headSha": "merge123",
        },
    ]))

    deploy = github.inspect_deploy(
        cwd="/repo",
        workflow="Deploy to Staging",
        commit_sha="merge123",
        runner=runner,
    )

    assert runner.calls == [
        ([
            "gh", "run", "list", "--workflow", "Deploy to Staging",
            "--commit", "merge123", "--json",
            "databaseId,status,conclusion,url,headSha", "--limit", "10",
        ], "/repo"),
    ]
    assert deploy.found is True
    assert deploy.completed is True
    assert deploy.successful is True
    assert deploy.failed is False
    assert deploy.run_id == 99


def test_inspect_deploy_returns_not_found_without_exact_commit_match():
    runner = FakeRunner(json.dumps([{
        "databaseId": 99,
        "status": "completed",
        "conclusion": "success",
        "url": "https://github.com/acme/luma/actions/runs/99",
        "headSha": "different",
    }]))

    deploy = github.inspect_deploy(
        cwd="/repo", workflow="Deploy", commit_sha="merge123", runner=runner)

    assert deploy.found is False
    assert deploy.successful is False
    assert deploy.failed is False


def test_inspect_deploy_treats_completed_non_success_as_failed():
    runner = FakeRunner(json.dumps([{
        "databaseId": 100,
        "status": "completed",
        "conclusion": "cancelled",
        "url": "https://github.com/acme/luma/actions/runs/100",
        "headSha": "merge123",
    }]))

    deploy = github.inspect_deploy(
        cwd="/repo", workflow="Deploy", commit_sha="merge123", runner=runner)

    assert deploy.completed is True
    assert deploy.successful is False
    assert deploy.failed is True
