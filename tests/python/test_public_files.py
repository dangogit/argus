import re
import subprocess
import sys
import importlib.util
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parents[2]

ALLOWED_TRACKED_LOCAL_CONFIG = {
    "tests/python/v2/fixtures/argus.yaml",
}

FORBIDDEN_TRACKED_PATTERNS = [
    re.compile(r"(^|/)\.DS_Store$"),
    re.compile(r"(^|/)Thumbs\.db$"),
    re.compile(r"(^|/)\.pytest_cache/"),
    re.compile(r"(^|/)__pycache__/"),
    re.compile(r"\.pyc$"),
    re.compile(r"(^|/)\.coverage$"),
    re.compile(r"(^|/)\.venv/"),
    re.compile(r"(^|/)node_modules/"),
    re.compile(r"(^|/)\.next/"),
    re.compile(r"(^|/)run/"),
    re.compile(r"(^|/)argus-run/"),
    re.compile(r"(^|/)\.env($|\.)(?!example$)"),
    re.compile(r"(^|/)argus\.yaml$"),
    re.compile(r"\.(key|pem|p12)$"),
]


def _tracked_files() -> list[str]:
    return subprocess.run(
        ["git", "ls-files"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()


def _load_public_launch_check():
    path = REPO / "scripts" / "public_launch_check.py"
    spec = importlib.util.spec_from_file_location("argus_public_launch_check", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_tracked_files_do_not_include_local_or_secret_artifacts():
    offenders = []
    for rel in _tracked_files():
        if rel in ALLOWED_TRACKED_LOCAL_CONFIG:
            continue
        if any(pattern.search(rel) for pattern in FORBIDDEN_TRACKED_PATTERNS):
            offenders.append(rel)

    assert offenders == []


def test_docker_compose_provides_pgvector_quickstart_database():
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    postgres = compose["services"]["postgres"]

    assert postgres["image"] == "pgvector/pgvector:pg17"
    assert postgres["environment"] == {
        "POSTGRES_USER": "argus",
        "POSTGRES_PASSWORD": "argus",
        "POSTGRES_DB": "argus",
    }
    assert "5440:5432" in postgres["ports"]
    assert postgres["healthcheck"]["test"] == [
        "CMD-SHELL",
        "pg_isready -U argus -d argus",
    ]


def test_dockerfile_provides_cli_image_without_local_state():
    dockerfile = (REPO / "Dockerfile").read_text()
    dockerignore = (REPO / ".dockerignore").read_text().splitlines()

    assert dockerfile.startswith("FROM python:3.12-slim")
    assert "ARGUS_RUN_ROOT=/var/lib/argus" in dockerfile
    assert "ARGUS_CONFIG=/config/argus.yaml" in dockerfile
    assert 'ENTRYPOINT ["argus"]' in dockerfile
    assert 'CMD ["--help"]' in dockerfile
    assert "USER argus" in dockerfile

    assert ".env" in dockerignore
    assert ".env.local" in dockerignore
    assert "argus.yaml" in dockerignore
    assert "run" in dockerignore
    assert ".venv" in dockerignore


def test_release_workflow_uses_trusted_pypi_publish():
    workflow = yaml.safe_load((REPO / ".github/workflows/release.yml").read_text())
    smoke = workflow["jobs"]["smoke-python-wheel"]
    sdist = workflow["jobs"]["smoke-python-sdist"]
    publish = workflow["jobs"]["publish-pypi"]
    smoke_run = "\n".join(
        step.get("run", "")
        for step in smoke["steps"]
        if isinstance(step, dict)
    )
    sdist_run = "\n".join(
        step.get("run", "")
        for step in sdist["steps"]
        if isinstance(step, dict)
    )
    publish_uses = [
        step["uses"]
        for step in publish["steps"]
        if isinstance(step, dict) and "uses" in step
    ]

    assert workflow["permissions"] == {"contents": "read"}
    assert smoke["needs"] == "build-python"
    assert smoke["services"]["postgres"]["image"] == "pgvector/pgvector:pg17"
    assert "dist/*.whl" in smoke_run
    assert "$RUNNER_TEMP/argus-wheel-smoke" in smoke_run
    assert "argus db migrate" in smoke_run
    assert "argus validate" in smoke_run
    assert "argus doctor" in smoke_run
    assert sdist["needs"] == "build-python"
    assert sdist["services"]["postgres"]["image"] == "pgvector/pgvector:pg17"
    assert "dist/*.tar.gz" in sdist_run
    assert "$RUNNER_TEMP/argus-sdist-smoke" in sdist_run
    assert "argus db migrate" in sdist_run
    assert "argus validate" in sdist_run
    assert "argus doctor" in sdist_run
    assert publish["if"] == "github.event_name == 'release'"
    assert publish["needs"] == [
        "build-python",
        "smoke-python-wheel",
        "smoke-python-sdist",
    ]
    assert publish["environment"] == {
        "name": "pypi",
        "url": "https://pypi.org/p/argus-agent",
    }
    assert publish["permissions"]["id-token"] == "write"
    assert "pypa/gh-action-pypi-publish@release/v1" in publish_uses


def test_workflow_triggers_parse_as_string_keys():
    for rel in [".github/workflows/ci.yml", ".github/workflows/release.yml"]:
        workflow = yaml.safe_load((REPO / rel).read_text())
        assert "on" in workflow
        assert True not in workflow


def test_public_community_health_files_are_present():
    required = {
        "CHANGELOG.md",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "Dockerfile",
        "LICENSE",
        "MANIFEST.in",
        "README.md",
        "SECURITY.md",
        "SUPPORT.md",
        "THIRD_PARTY_NOTICES.md",
        "VISION.md",
        "scripts/public_launch_check.py",
        "scripts/install.ps1",
        ".dockerignore",
        ".github/ISSUE_TEMPLATE/bug_report.md",
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/ISSUE_TEMPLATE/feature_request.md",
        ".github/ISSUE_TEMPLATE/setup_question.md",
        ".github/PULL_REQUEST_TEMPLATE.md",
        ".github/dependabot.yml",
    }

    tracked = set(_tracked_files())
    assert required <= tracked


def test_dependabot_covers_public_dependency_surfaces():
    config = yaml.safe_load((REPO / ".github/dependabot.yml").read_text())
    ecosystems = {
        (entry["package-ecosystem"], entry["directory"])
        for entry in config["updates"]
    }

    assert ecosystems == {
        ("github-actions", "/"),
        ("pip", "/"),
        ("npm", "/dashboard"),
    }


def test_setup_question_template_requests_redacted_operational_proof():
    text = (REPO / ".github/ISSUE_TEMPLATE/setup_question.md").read_text()

    assert "https://github.com/dangogit/argus/discussions/new/choose" in text
    assert "argus doctor --deep --live --json" in text
    assert "argus go-live" in text
    assert "No tokens, passwords, signing secrets, or webhook secrets." in text
    assert "chat-only" in text
    assert "monitor-only" in text
    assert "pm-propose-pr" in text


def test_issue_config_routes_setup_questions_to_discussions():
    config = yaml.safe_load((REPO / ".github/ISSUE_TEMPLATE/config.yml").read_text())
    links = {entry["name"]: entry["url"] for entry in config["contact_links"]}

    assert links["Setup question"] == (
        "https://github.com/dangogit/argus/discussions/new/choose"
    )


def test_llms_index_covers_public_install_support_and_external_checks():
    text = (REPO / "llms.txt").read_text()

    assert "https://raw.githubusercontent.com/dangogit/argus/main/scripts/install.sh" in text
    assert "https://raw.githubusercontent.com/dangogit/argus/main/scripts/install.ps1" in text
    assert "https://github.com/dangogit/argus/discussions" in text
    assert "SUPPORT.md" in text
    assert "gh repo view dangogit/argus" in text
    assert "gh api repos/dangogit/argus/community/profile" in text
    assert "python scripts/public_launch_check.py --repo dangogit/argus" in text
    assert "package.pypi" in text
    assert "zero-step Actions failures" in text


def test_public_launch_checklist_covers_external_github_proof():
    text = (REPO / "docs/public-launch.md").read_text()

    assert "python scripts/public_launch_check.py --repo dangogit/argus" in text
    assert "The script exits `1`" in text
    assert "gh repo view dangogit/argus" in text
    assert "gh api repos/dangogit/argus/pages" in text
    assert "gh api repos/dangogit/argus/community/profile" in text
    assert "gh release view --repo dangogit/argus" in text
    assert "Your current plan does not support GitHub Pages" in text
    assert "`isPrivate` and `private` are `false`." in text
    assert "Community health is `100`." in text


def test_public_launch_check_reports_all_clear_for_public_state():
    checker = _load_public_launch_check()
    state = {
        "repo_view": {
            "isPrivate": False,
            "hasIssuesEnabled": True,
            "hasDiscussionsEnabled": True,
            "homepageUrl": "https://docs.argus.dev",
            "repositoryTopics": [
                {"name": name} for name in checker.DEFAULT_REQUIRED_TOPICS
            ],
        },
        "repo_api": {"private": False, "has_pages": True},
        "pages": {"html_url": "https://docs.argus.dev"},
        "community": {"health_percentage": 100},
        "release": {
            "tagName": "v0.2.0",
            "assets": [
                {"name": "argus_agent-0.2.0-py3-none-any.whl"},
                {"name": "argus_agent-0.2.0.tar.gz"},
            ],
        },
        "runs": [
            {
                "workflowName": "CI",
                "headBranch": "main",
                "conclusion": "success",
                "createdAt": "2026-06-26T00:00:00Z",
            },
            {
                "workflowName": "Release",
                "headBranch": "main",
                "conclusion": "success",
                "createdAt": "2026-06-26T00:00:00Z",
            }
        ],
    }

    checks = checker.evaluate_checks(
        state,
        pypi={"package": "argus-agent", "ok": True, "version": "0.2.0"},
    )

    assert {check.status for check in checks} == {"ok"}


def test_public_launch_check_reports_external_blockers():
    checker = _load_public_launch_check()
    state = {
        "repo_view": {
            "isPrivate": True,
            "hasIssuesEnabled": True,
            "hasDiscussionsEnabled": False,
            "homepageUrl": "https://github.com/dangogit/argus#readme",
            "repositoryTopics": [{"name": "ai-agents"}],
        },
        "repo_api": {"private": True, "has_pages": False},
        "community": {"health_percentage": 75},
        "release": {
            "tagName": "v0.2.0",
            "assets": [{"name": "argus_agent-0.2.0-py3-none-any.whl"}],
        },
        "runs": [
            {
                "workflowName": "CI",
                "headBranch": "main",
                "conclusion": "failure",
                "createdAt": "2026-06-26T00:00:00Z",
            },
            {
                "workflowName": ".github/workflows/release.yml",
                "headBranch": "main",
                "conclusion": "failure",
                "createdAt": "2026-06-26T00:00:01Z",
            }
        ],
        "failed_run_details": [{"databaseId": "1", "jobs": [{"steps": []}]}],
    }

    checks = checker.evaluate_checks(
        state,
        pypi={"package": "argus-agent", "ok": False, "error": "HTTP 404"},
    )
    by_name = {check.name: check for check in checks}

    assert by_name["repo.visibility"].status == "blocked"
    assert by_name["repo.discussions"].status == "blocked"
    assert by_name["repo.topics"].status == "blocked"
    assert by_name["docs.homepage"].status == "blocked"
    assert by_name["community.health"].status == "blocked"
    assert by_name["release.assets"].status == "blocked"
    assert by_name["actions.ci"].status == "blocked"
    assert "failed before job steps" in by_name["actions.ci"].detail


def test_public_launch_check_does_not_count_dependabot_as_ci():
    checker = _load_public_launch_check()
    state = {
        "repo_view": {
            "isPrivate": False,
            "hasIssuesEnabled": True,
            "hasDiscussionsEnabled": True,
            "homepageUrl": "https://docs.argus.dev",
            "repositoryTopics": [
                {"name": name} for name in checker.DEFAULT_REQUIRED_TOPICS
            ],
        },
        "repo_api": {"private": False, "has_pages": True},
        "pages": {"html_url": "https://docs.argus.dev"},
        "community": {"health_percentage": 100},
        "release": {
            "tagName": "v0.2.0",
            "assets": [
                {"name": "argus_agent-0.2.0-py3-none-any.whl"},
                {"name": "argus_agent-0.2.0.tar.gz"},
            ],
        },
        "runs": [
            {
                "workflowName": "Dependabot Updates",
                "headBranch": "main",
                "conclusion": "success",
                "createdAt": "2026-06-26T00:00:00Z",
            },
        ],
    }

    checks = checker.evaluate_checks(state)
    by_name = {check.name: check for check in checks}

    assert by_name["actions.ci"].status == "blocked"
    assert by_name["actions.release"].status == "blocked"


def test_third_party_notices_cover_runtime_dependencies():
    text = (REPO / "THIRD_PARTY_NOTICES.md").read_text()

    for package in ["psycopg", "pydantic", "PyYAML", "httpx"]:
        assert f"`{package}`" in text

    assert "LGPL-3.0-only" in text
    assert "BSD-3-Clause" in text
    assert "MPL-2.0" in text
    assert "does not vendor these packages" in text


def test_windows_installer_is_documented_and_uses_safe_defaults():
    installer = (REPO / "scripts/install.ps1").read_text()
    readme = (REPO / "README.md").read_text()
    quickstart = (REPO / "docs/quickstart.md").read_text()

    assert "$ErrorActionPreference = \"Stop\"" in installer
    assert "Python 3.11+" in installer
    assert "git not found" in installer
    assert '"pipx", "install", "--force"' in installer
    assert "ARGUS_REPO_URL" in installer
    assert "ARGUS_PACKAGE_SPEC" in installer
    assert "ARGUS_PYTHON" in installer
    assert "irm https://raw.githubusercontent.com/dangogit/argus/main/scripts/install.ps1 | iex" in readme
    assert "Git for Windows" in quickstart
