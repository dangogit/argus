#!/usr/bin/env python3
"""Check external public-launch state that cannot be proven from git files."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_REQUIRED_TOPICS = {
    "ai-agent",
    "ai-agents",
    "agentic-workflows",
    "developer-tools",
    "self-hosted",
    "monitoring",
    "slack",
    "telegram",
    "codex",
    "claude",
    "python",
}
DEFAULT_REQUIRED_ASSETS = (".whl", ".tar.gz")


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _run_json(args: list[str]) -> tuple[int, Any | None, str]:
    completed = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
    )
    stderr = completed.stderr.strip()
    stdout = completed.stdout.strip()
    if completed.returncode != 0:
        return completed.returncode, None, stderr or stdout
    if not stdout:
        return completed.returncode, None, ""
    try:
        return completed.returncode, json.loads(stdout), ""
    except json.JSONDecodeError as exc:
        return completed.returncode, None, f"invalid JSON from {' '.join(args)}: {exc}"


def _github_state(repo: str, inspect_failed_runs: int) -> dict[str, Any]:
    state: dict[str, Any] = {"repo": repo, "errors": []}

    _, view, err = _run_json(
        [
            "gh",
            "repo",
            "view",
            repo,
            "--json",
            ",".join(
                [
                    "description",
                    "homepageUrl",
                    "isPrivate",
                    "hasDiscussionsEnabled",
                    "hasIssuesEnabled",
                    "latestRelease",
                    "repositoryTopics",
                ]
            ),
        ]
    )
    state["repo_view"] = view
    if err:
        state["errors"].append({"command": "gh repo view", "error": err})

    _, repo_api, err = _run_json(["gh", "api", f"repos/{repo}"])
    state["repo_api"] = repo_api
    if err:
        state["errors"].append({"command": "gh api repos", "error": err})

    _, pages, err = _run_json(["gh", "api", f"repos/{repo}/pages"])
    state["pages"] = pages
    if err:
        state["pages_error"] = err

    _, community, err = _run_json(["gh", "api", f"repos/{repo}/community/profile"])
    state["community"] = community
    if err:
        state["errors"].append({"command": "gh api community/profile", "error": err})

    _, release, err = _run_json(
        [
            "gh",
            "release",
            "view",
            "--repo",
            repo,
            "--json",
            "tagName,name,publishedAt,url,assets",
        ]
    )
    state["release"] = release
    if err:
        state["release_error"] = err

    _, runs, err = _run_json(
        [
            "gh",
            "run",
            "list",
            "--repo",
            repo,
            "--limit",
            "10",
            "--json",
            "databaseId,workflowName,status,conclusion,event,headBranch,createdAt,url",
        ]
    )
    state["runs"] = runs or []
    if err:
        state["runs_error"] = err

    failed_run_details = []
    for run in (runs or [])[:inspect_failed_runs]:
        if run.get("conclusion") != "failure":
            continue
        run_id = str(run.get("databaseId"))
        _, details, detail_err = _run_json(
            ["gh", "run", "view", run_id, "--repo", repo, "--json", "jobs"]
        )
        if detail_err:
            failed_run_details.append({"databaseId": run_id, "error": detail_err})
        else:
            failed_run_details.append({"databaseId": run_id, "jobs": details.get("jobs", [])})
    state["failed_run_details"] = failed_run_details

    return state


def _pypi_state(package: str | None) -> dict[str, Any] | None:
    if not package:
        return None
    url = f"https://pypi.org/pypi/{package}/json"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return {
            "package": package,
            "url": url,
            "ok": True,
            "version": payload.get("info", {}).get("version"),
        }
    except urllib.error.HTTPError as exc:
        return {"package": package, "url": url, "ok": False, "error": f"HTTP {exc.code}"}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"package": package, "url": url, "ok": False, "error": str(exc)}


def _topic_names(repo_view: dict[str, Any] | None) -> set[str]:
    if not repo_view:
        return set()
    return {entry.get("name", "") for entry in repo_view.get("repositoryTopics", [])}


def _release_asset_names(release: dict[str, Any] | None) -> list[str]:
    if not release:
        return []
    return [asset.get("name", "") for asset in release.get("assets", [])]


def _latest_ci_run(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    for run in runs:
        if run.get("workflowName") == "CI" and run.get("headBranch") == "main":
            return run
    for run in runs:
        if run.get("workflowName") == "CI":
            return run
    return None


def _latest_release_run(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    release_names = {"Release", ".github/workflows/release.yml", "release.yml"}
    for run in runs:
        if run.get("workflowName") in release_names:
            return run
    return None


def _failed_run_has_no_steps(state: dict[str, Any]) -> bool:
    for detail in state.get("failed_run_details", []):
        jobs = detail.get("jobs")
        if jobs == []:
            return True
        if isinstance(jobs, list) and any(job.get("steps") == [] for job in jobs):
            return True
    return False


def evaluate_checks(
    state: dict[str, Any],
    *,
    required_topics: set[str] = DEFAULT_REQUIRED_TOPICS,
    required_assets: tuple[str, ...] = DEFAULT_REQUIRED_ASSETS,
    pypi: dict[str, Any] | None = None,
) -> list[Check]:
    repo_view = state.get("repo_view") or {}
    repo_api = state.get("repo_api") or {}
    community = state.get("community") or {}
    release = state.get("release")
    runs = state.get("runs") or []
    checks: list[Check] = []

    if state.get("errors"):
        checks.append(
            Check(
                "github.api",
                "blocked",
                "; ".join(error["error"] for error in state["errors"]),
            )
        )

    private_values = [repo_view.get("isPrivate"), repo_api.get("private")]
    if any(value is True for value in private_values):
        checks.append(Check("repo.visibility", "blocked", "repository is private"))
    else:
        checks.append(Check("repo.visibility", "ok", "repository is public"))

    if repo_view.get("hasIssuesEnabled"):
        checks.append(Check("repo.issues", "ok", "issues enabled"))
    else:
        checks.append(Check("repo.issues", "blocked", "issues disabled"))

    if repo_view.get("hasDiscussionsEnabled"):
        checks.append(Check("repo.discussions", "ok", "discussions enabled"))
    else:
        checks.append(Check("repo.discussions", "blocked", "discussions disabled"))

    missing_topics = sorted(required_topics - _topic_names(repo_view))
    if missing_topics:
        checks.append(Check("repo.topics", "blocked", "missing " + ", ".join(missing_topics)))
    else:
        checks.append(Check("repo.topics", "ok", "required topics present"))

    homepage = repo_view.get("homepageUrl") or repo_api.get("homepage") or ""
    has_pages = bool(repo_api.get("has_pages") or state.get("pages"))
    homepage_is_docs_host = homepage.startswith("http") and "github.com/" not in homepage
    homepage_is_repo_docs = "github.com/dangogit/argus" in homepage and (
        "/tree/main/docs" in homepage or "/blob/main/docs" in homepage
    )
    if has_pages or homepage_is_docs_host or homepage_is_repo_docs:
        checks.append(Check("docs.homepage", "ok", homepage or "GitHub Pages enabled"))
    elif homepage:
        checks.append(Check("docs.homepage", "blocked", f"not hosted docs: {homepage}"))
    else:
        checks.append(Check("docs.homepage", "blocked", "homepage URL missing"))

    health = community.get("health_percentage")
    if isinstance(health, int) and health >= 100:
        checks.append(Check("community.health", "ok", f"{health}%"))
    elif health is None:
        checks.append(Check("community.health", "blocked", "community profile unavailable"))
    else:
        checks.append(Check("community.health", "blocked", f"{health}%"))

    asset_names = _release_asset_names(release)
    if not release:
        checks.append(Check("release.latest", "blocked", state.get("release_error", "no release")))
    else:
        missing_assets = [
            suffix for suffix in required_assets if not any(name.endswith(suffix) for name in asset_names)
        ]
        if missing_assets:
            checks.append(
                Check(
                    "release.assets",
                    "blocked",
                    "missing assets ending with " + ", ".join(missing_assets),
                )
            )
        else:
            checks.append(
                Check(
                    "release.assets",
                    "ok",
                    f"{release.get('tagName')} assets={len(asset_names)}",
                )
            )

    latest_ci = _latest_ci_run(runs)
    if not latest_ci:
        checks.append(Check("actions.ci", "blocked", state.get("runs_error", "no CI runs")))
    elif latest_ci.get("conclusion") == "success":
        checks.append(Check("actions.ci", "ok", f"{latest_ci.get('createdAt')} success"))
    else:
        detail = (
            f"{latest_ci.get('createdAt')} {latest_ci.get('workflowName')} "
            f"{latest_ci.get('conclusion') or latest_ci.get('status')}"
        )
        if _failed_run_has_no_steps(state):
            detail += "; failed before job steps"
        checks.append(Check("actions.ci", "blocked", detail))

    latest_release_run = _latest_release_run(runs)
    if not latest_release_run:
        checks.append(Check("actions.release", "blocked", "no Release workflow runs"))
    elif latest_release_run.get("conclusion") == "success":
        checks.append(
            Check("actions.release", "ok", f"{latest_release_run.get('createdAt')} success")
        )
    else:
        detail = (
            f"{latest_release_run.get('createdAt')} "
            f"{latest_release_run.get('workflowName')} "
            f"{latest_release_run.get('conclusion') or latest_release_run.get('status')}"
        )
        if _failed_run_has_no_steps(state):
            detail += "; failed before job steps"
        checks.append(Check("actions.release", "blocked", detail))

    if pypi is not None:
        if pypi.get("ok"):
            checks.append(Check("package.pypi", "ok", f"{pypi['package']} {pypi.get('version')}"))
        else:
            checks.append(
                Check(
                    "package.pypi",
                    "blocked",
                    f"{pypi['package']} unavailable: {pypi.get('error', 'unknown error')}",
                )
            )

    return checks


def _render_table(checks: list[Check]) -> str:
    rows = ["check                       status    detail"]
    rows.append("-------------------------- --------- ----------------------------------------")
    for check in checks:
        rows.append(f"{check.name:<26} {check.status:<9} {check.detail}")
    overall = "blocked" if any(check.status == "blocked" for check in checks) else "ok"
    rows.append(f"overall                    {overall}")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check external Argus public-launch state with GitHub and PyPI.",
    )
    parser.add_argument("--repo", default="dangogit/argus", help="GitHub repo, owner/name")
    parser.add_argument(
        "--pypi-package",
        default="argus-agent",
        help="PyPI package to verify, or empty string to skip",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of a table",
    )
    parser.add_argument(
        "--inspect-failed-runs",
        type=int,
        default=3,
        help="Inspect this many recent failed runs for zero-step failures",
    )
    parser.add_argument(
        "--no-pypi",
        action="store_true",
        help="Skip PyPI package availability check",
    )
    args = parser.parse_args(argv)

    if shutil.which("gh") is None:
        print("argus public launch check: gh CLI not found", file=sys.stderr)
        return 2

    state = _github_state(args.repo, max(args.inspect_failed_runs, 0))
    pypi_package = None if args.no_pypi else (args.pypi_package or None)
    pypi = _pypi_state(pypi_package)
    checks = evaluate_checks(state, pypi=pypi)
    blocked = [check for check in checks if check.status == "blocked"]

    if args.json:
        print(
            json.dumps(
                {
                    "repo": args.repo,
                    "status": "blocked" if blocked else "ok",
                    "checks": [check.__dict__ for check in checks],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(_render_table(checks))

    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
