import json

from argus.v2.ownership import vercel


SHA = "a" * 40
OTHER_SHA = "b" * 40
DEPLOYMENT_URL = "https://tadam-agents-abc-tadam-technology.vercel.app"


class FakeRunner:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, argv, cwd=None):
        self.calls.append((argv, cwd))
        return self.response


def _payload(*, state="READY", sha=SHA, project="tadam-agents"):
    return {
        "contextName": "tadam-technology",
        "deployments": [{
            "name": project,
            "url": DEPLOYMENT_URL.removeprefix("https://"),
            "state": state,
            "createdAt": 1783418164414,
            "meta": {
                "githubCommitSha": sha,
                "githubCommitRef": "staging",
            },
        }],
    }


def test_inspect_deploy_proves_ready_vercel_deployment_for_exact_commit():
    runner = FakeRunner(json.dumps(_payload()))

    deploy = vercel.inspect_deploy(
        cwd="/repo",
        project="tadam-agents",
        scope="tadam-technology",
        commit_sha=SHA,
        expected_branch="staging",
        runner=runner,
    )

    assert runner.calls == [
        ([
            "vercel", "list", "tadam-agents",
            "--scope", "tadam-technology",
            "--meta", f"githubCommitSha={SHA}",
            "--meta", "githubCommitRef=staging",
            "--format", "json", "--yes",
        ], "/repo"),
    ]
    assert deploy.found is True
    assert deploy.completed is True
    assert deploy.successful is True
    assert deploy.failed is False
    assert deploy.url == DEPLOYMENT_URL
    assert deploy.head_sha == SHA
    assert deploy.branch == "staging"


def test_inspect_deploy_rejects_row_without_exact_commit_match():
    runner = FakeRunner(json.dumps(_payload(sha=OTHER_SHA)))

    deploy = vercel.inspect_deploy(
        cwd="/repo", project="tadam-agents", scope="tadam-technology",
        commit_sha=SHA, expected_branch="staging", runner=runner,
    )

    assert deploy.found is False
    assert deploy.successful is False
    assert deploy.failed is False


def test_inspect_deploy_rejects_wrong_project_or_scope():
    wrong_project = FakeRunner(json.dumps(_payload(project="other-project")))
    wrong_scope = FakeRunner(json.dumps({**_payload(), "contextName": "other-team"}))

    project_deploy = vercel.inspect_deploy(
        cwd="/repo", project="tadam-agents", scope="tadam-technology",
        commit_sha=SHA, expected_branch="staging", runner=wrong_project,
    )
    scope_deploy = vercel.inspect_deploy(
        cwd="/repo", project="tadam-agents", scope="tadam-technology",
        commit_sha=SHA, expected_branch="staging", runner=wrong_scope,
    )

    assert project_deploy.found is False
    assert scope_deploy.found is False


def test_inspect_deploy_rejects_same_commit_from_wrong_branch():
    payload = _payload()
    payload["deployments"][0]["meta"]["githubCommitRef"] = "main"
    runner = FakeRunner(json.dumps(payload))

    deploy = vercel.inspect_deploy(
        cwd="/repo", project="tadam-agents", scope="tadam-technology",
        commit_sha=SHA, expected_branch="staging", runner=runner,
    )

    assert deploy.found is False


def test_inspect_deploy_maps_vercel_terminal_failure():
    runner = FakeRunner(json.dumps(_payload(state="ERROR")))

    deploy = vercel.inspect_deploy(
        cwd="/repo", project="tadam-agents", scope="tadam-technology",
        commit_sha=SHA, expected_branch="staging", runner=runner,
    )

    assert deploy.completed is True
    assert deploy.successful is False
    assert deploy.failed is True
    assert deploy.status == "COMPLETED"
    assert deploy.conclusion == "FAILURE"


def test_inspect_deploy_keeps_building_deployment_pending():
    runner = FakeRunner(json.dumps(_payload(state="BUILDING")))

    deploy = vercel.inspect_deploy(
        cwd="/repo", project="tadam-agents", scope="tadam-technology",
        commit_sha=SHA, expected_branch="staging", runner=runner,
    )

    assert deploy.found is True
    assert deploy.completed is False
    assert deploy.successful is False
    assert deploy.failed is False
    assert deploy.status == "IN_PROGRESS"
    assert deploy.conclusion == "UNKNOWN"
