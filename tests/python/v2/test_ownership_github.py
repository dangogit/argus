import json

import pytest

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
        "headRefOid": "abc1234",
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
        mergeCommit={"oid": "def1234"},
    )))

    pr = github.inspect_pr(cwd="/repo", pr_ref="42", runner=runner)

    assert pr.state == "MERGED"
    assert pr.merge_sha == "def1234"
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


def test_inspect_pr_normalizes_provider_string_whitespace():
    runner = FakeRunner(json.dumps(_pr_payload(
        url="  https://github.com/acme/luma/pull/42  ",
        baseRefName="  staging  ",
        headRefName="  argus/req-1  ",
        headRefOid="  abc1234  ",
        files=[{"path": "  src/App.tsx  "}],
        statusCheckRollup=[{
            "name": "  test  ",
            "status": "  COMPLETED  ",
            "conclusion": "  SUCCESS  ",
        }],
    )))

    pr = github.inspect_pr(cwd="/repo", pr_ref="42", runner=runner)

    assert pr.url == "https://github.com/acme/luma/pull/42"
    assert pr.base == "staging"
    assert pr.head == "argus/req-1"
    assert pr.head_sha == "abc1234"
    assert pr.changed_files == ("src/App.tsx",)
    assert pr.checks == ("test",)
    assert pr.checks_passed is True


@pytest.mark.parametrize(("field", "value", "attribute"), [
    ("url", "   ", "url"),
    ("headRefOid", "   ", "head_sha"),
    ("baseRefName", "   ", "base"),
    ("headRefName", "   ", "head"),
])
def test_inspect_pr_rejects_blank_provider_fields(field, value, attribute):
    runner = FakeRunner(json.dumps(_pr_payload(**{field: value})))

    pr = github.inspect_pr(cwd="/repo", pr_ref="42", runner=runner)

    assert getattr(pr, attribute) == ""


@pytest.mark.parametrize("url", [
    "http://github.com/acme/luma/pull/42",
    "https:///acme/luma/pull/42",
    "https://github.com/acme/luma/issues/42",
    "https://github.com/acme/luma/pull/43",
])
def test_inspect_pr_rejects_malformed_or_mismatched_url(url):
    runner = FakeRunner(json.dumps(_pr_payload(url=url)))

    pr = github.inspect_pr(cwd="/repo", pr_ref="42", runner=runner)

    assert pr.url == ""


@pytest.mark.parametrize("sha", ["abc123", "not-a-hex-sha", "g123456"])
def test_inspect_pr_rejects_invalid_head_sha(sha):
    runner = FakeRunner(json.dumps(_pr_payload(headRefOid=sha)))

    pr = github.inspect_pr(cwd="/repo", pr_ref="42", runner=runner)

    assert pr.head_sha == ""


@pytest.mark.parametrize("path", [
    "   ",
    "/etc/passwd",
    "src/../secret.txt",
    "./src/App.tsx",
    "src/\x00App.tsx",
])
def test_inspect_pr_rejects_invalid_repository_path(path):
    runner = FakeRunner(json.dumps(_pr_payload(files=[{"path": path}])))

    pr = github.inspect_pr(cwd="/repo", pr_ref="42", runner=runner)

    assert pr.changed_files == ()


def test_inspect_pr_rejects_blank_check_name():
    runner = FakeRunner(json.dumps(_pr_payload(statusCheckRollup=[{
        "name": "   ", "status": "COMPLETED", "conclusion": "SUCCESS",
    }])))

    pr = github.inspect_pr(cwd="/repo", pr_ref="42", runner=runner)

    assert pr.checks == ()
    assert pr.checks_passed is False


def test_inspect_pr_accepts_github_enterprise_https_pr_url():
    url = "https://github.corp.example/acme/luma/pull/42"
    runner = FakeRunner(json.dumps(_pr_payload(url=url)))

    pr = github.inspect_pr(cwd="/repo", pr_ref="42", runner=runner)

    assert pr.url == url


def test_inspect_pr_derives_repo_from_github_enterprise_url_without_checkout():
    url = "https://github.corp.example/acme/luma/pull/42"
    runner = FakeRunner(json.dumps(_pr_payload(url=url)))

    github.inspect_pr(cwd=None, pr_ref=url, runner=runner)

    assert runner.calls == [
        ([
            "gh", "pr", "view", "42", "--repo", "acme/luma",
            "--json", PR_FIELDS,
        ], None),
    ]


def test_inspect_deploy_parses_matching_run_with_exact_request():
    runner = FakeRunner(json.dumps([
        {
            "databaseId": 99,
            "status": "completed",
            "conclusion": "success",
            "url": "https://github.com/acme/luma/actions/runs/99",
            "headSha": "def1234",
        },
    ]))

    deploy = github.inspect_deploy(
        cwd="/repo",
        workflow="Deploy to Staging",
        commit_sha="def1234",
        runner=runner,
    )

    assert runner.calls == [
        ([
            "gh", "run", "list", "--workflow", "Deploy to Staging",
            "--commit", "def1234", "--json",
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
        "headSha": "abc9876",
    }]))

    deploy = github.inspect_deploy(
        cwd="/repo", workflow="Deploy", commit_sha="def1234", runner=runner)

    assert deploy.found is False
    assert deploy.successful is False
    assert deploy.failed is False


def test_inspect_deploy_treats_completed_non_success_as_failed():
    runner = FakeRunner(json.dumps([{
        "databaseId": 100,
        "status": "completed",
        "conclusion": "cancelled",
        "url": "https://github.com/acme/luma/actions/runs/100",
        "headSha": "def1234",
    }]))

    deploy = github.inspect_deploy(
        cwd="/repo", workflow="Deploy", commit_sha="def1234", runner=runner)

    assert deploy.completed is True
    assert deploy.successful is False
    assert deploy.failed is True
