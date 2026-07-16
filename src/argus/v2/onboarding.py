"""Project onboarding scanner and artifact writer."""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MODES = ("chat-only", "monitor-only", "pm-propose-pr")
CONNECTOR_ENV = {
    "vercel": ("VERCEL_TOKEN",),
    "vercel_events": ("VERCEL_TOKEN",),
    "supabase": ("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_URL"),
    "sentry": ("SENTRY_AUTH_TOKEN", "SENTRY_ORG", "SENTRY_PROJECT"),
    "firebase": ("GOOGLE_APPLICATION_CREDENTIALS", "FIREBASE_PROJECT_ID"),
    "github": ("GITHUB_TOKEN",),
    "fly": ("FLY_API_TOKEN",),
    "posthog": ("POSTHOG_PERSONAL_API_KEY", "POSTHOG_PROJECT_ID", "POSTHOG_HOST"),
    "postgres": ("DATABASE_URL",),
    "openapi": ("OPENAPI_URL",),
    "webhook": ("ARGUS_WEBHOOK_SOURCE_URL",),
    "uptime": ("ARGUS_UPTIME_URL",),
}

CONNECTOR_NOTES = {
    "sentry": (
        "Argus polls the Sentry Issues API. A DSN is not enough; configure "
        "org, project, and an API token with issue read access."
    ),
    "posthog": (
        "Argus polls PostHog activity or error-tracking APIs. "
        "`NEXT_PUBLIC_POSTHOG_KEY` is not enough; configure a personal API "
        "key, numeric project id, and host."
    ),
}


@dataclass
class ProjectScan:
    path: Path
    slug: str
    docs: list[str] = field(default_factory=list)
    package_files: list[str] = field(default_factory=list)
    ci_workflows: list[str] = field(default_factory=list)
    vercel_projects: list[str] = field(default_factory=list)
    env_keys: list[str] = field(default_factory=list)
    provider_hints: list[str] = field(default_factory=list)
    test_command: str | None = None
    setup_command: str | None = None
    git_remote: str | None = None
    github_repo: str | None = None
    base_branch: str = "main"


def scan_project(path: Path) -> ProjectScan:
    repo = Path(path).expanduser().resolve()
    slug = _slug(repo.name)
    scan = ProjectScan(path=repo, slug=slug)
    for name in ("AGENTS.md", "CLAUDE.md", "README.md", "README"):
        if (repo / name).exists():
            scan.docs.append(name)
    scan.package_files = [name for name in (
        "package.json", "pnpm-lock.yaml", "yarn.lock", "package-lock.json",
        "pyproject.toml", "requirements.txt", "uv.lock", "Cargo.toml",
        "go.mod",
    ) if (repo / name).exists()]
    workflows = repo / ".github" / "workflows"
    if workflows.exists():
        scan.ci_workflows = [
            str(path.relative_to(repo))
            for path in sorted(workflows.glob("*.y*ml"))
        ]
    scan.vercel_projects = _vercel_projects(repo)
    scan.env_keys = _env_keys(repo)
    scan.provider_hints = _provider_hints(
        repo, set(scan.env_keys), vercel_projects=scan.vercel_projects)
    scan.test_command, scan.setup_command = _commands(repo)
    scan.git_remote = _git(repo, "remote", "get-url", "origin")
    scan.github_repo = _github_repo(scan.git_remote)
    scan.base_branch = _base_branch(repo)
    return scan


def write_project_onboarding(
    project_path: Path,
    *,
    config_path: Path,
    out_dir: Path,
    mode: str = "chat-only",
    channel: str = "cli",
    channel_id: str | None = None,
    team_name: str | None = None,
    manager_engine: str = "codex",
    force: bool = False,
) -> dict[str, Path]:
    if mode not in MODES:
        raise ValueError(f"unsupported onboarding mode: {mode}")
    scan = scan_project(project_path)
    team = team_name or scan.slug
    out_dir = Path(out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    config_path = Path(config_path).expanduser()
    raw = _config_doc(scan, mode=mode, channel=channel, channel_id=channel_id,
                      team_name=team, manager_engine=manager_engine)
    if config_path.exists() and not force:
        raw = _merge_config(config_path, raw, team)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(_dump_yaml(raw), encoding="utf-8")
    env_path = out_dir / "argus.env.example.generated"
    env_path.write_text(_env_example(scan, channel), encoding="utf-8")
    checklist_path = out_dir / "argus.onboarding.md"
    checklist_path.write_text(_checklist(scan, mode=mode, team_name=team,
                                         config_path=config_path,
                                         channel=channel), encoding="utf-8")
    return {
        "config": config_path,
        "env": env_path,
        "checklist": checklist_path,
    }


def _config_doc(scan: ProjectScan, *, mode: str, channel: str,
                channel_id: str | None, team_name: str,
                manager_engine: str) -> dict[str, Any]:
    developer_engine = "codex" if mode == "pm-propose-pr" else "echo"
    roles = [
        {
            "name": "manager",
            "kind": "front",
            "prompt": (
                "You are the Argus manager for this project. Triage chat, "
                "summarize status, and only dispatch implementation work when "
                "the configured mode allows it."
            ),
            "engine": {"engine": manager_engine},
        },
        {
            "name": "developer",
            "kind": "builder",
            "prompt": (
                "Implement scoped fixes and report concise evidence. End with "
                "ARGUS_RESULT: {\"ready\": true, \"summary\": \"...\"} when "
                "the diff is ready, or ARGUS_RESULT: {\"ready\": false, "
                "\"status\": \"blocked\", \"analysis\": \"...\"} when blocked."
            ),
            "engine": {"engine": developer_engine},
        },
    ]
    stages = ["developer"]
    if mode == "pm-propose-pr":
        roles += [
            {
                "name": "qa",
                "kind": "judge",
                "prompt": (
                    "Run or inspect verification and reject unsafe fixes. "
                    "QA-sensitive work cannot close unless the transcript "
                    "documents the verification path, every covered report or "
                    "item, and the post-fix follow-up condition. Protected UI "
                    "QA tasks cannot claim manual verification is runnable "
                    "unless the transcript records a working preview login "
                    "path: preview URL, login route or steps, non-secret "
                    "credential source or test account label, and observed "
                    "post-login page or state. End "
                    "failures with a classification of code regression, "
                    "environment blocker, expected cancellation, stale status, "
                    "or unknown. End "
                    "with ARGUS_RESULT: {\"verdict\": \"pass\", \"summary\": "
                    "\"...\"} or ARGUS_RESULT: {\"verdict\": \"fail\", "
                    "\"summary\": \"<classification plus evidence>\"}."
                ),
                "engine": {"engine": "codex"},
            },
            {
                "name": "senior",
                "kind": "judge",
                "prompt": (
                    "Review scope, risk, and PR readiness. QA-sensitive work "
                    "cannot close unless the transcript documents the "
                    "verification path, every covered report or item, and the "
                    "post-fix follow-up condition. Protected UI QA tasks "
                    "cannot claim manual verification is runnable unless the "
                    "transcript records a working preview login path: preview "
                    "URL, login route or steps, non-secret credential source "
                    "or test account label, and observed post-login page or "
                    "state. "
                    "Before a failing PR summary, classify each failure as code "
                    "regression, "
                    "environment blocker, expected cancellation, stale status, "
                    "or unknown. End with "
                    "ARGUS_RESULT: {\"decision\": \"approve\", \"summary\": "
                    "\"...\"} or ARGUS_RESULT: {\"decision\": \"changes\", "
                    "\"summary\": \"<classification plus evidence>\"}."
                ),
                "engine": {"engine": "codex"},
            },
        ]
        stages = ["developer", "qa", "senior"]
    project: dict[str, Any] = {
        "repo": str(scan.path),
        "base_branch": scan.base_branch,
        "work_branch_prefix": "argus",
        "allow_code_mode": True,
        "allow_network": True,
        "autofix": {
            "mode": "propose-pr",
            "draft": True,
            "force_draft_on_fail": True,
        },
        "pm": {
            "daily_limit": 1,
            "max_rework_attempts": 1,
        },
    }
    if scan.test_command:
        project["test_cmd"] = scan.test_command
    if scan.setup_command:
        project["setup_cmd"] = scan.setup_command
    if scan.github_repo:
        project["github_repo"] = scan.github_repo
    team = {
        "name": team_name,
        "project": project,
        "roles": roles,
        "pipeline": {"stages": stages, "max_iters": 1},
        "sources": [],
        "channels": _channels(channel, channel_id),
    }
    defaults: dict[str, Any] = {
        "engine": {"engine": "echo"},
        "autonomy": {
            "reversible_internal": "auto",
            "personal_outward": "approval",
            "irreversible_outward": "approval",
        },
    }
    if channel in {"slack", "telegram", "whatsapp"}:
        defaults["webhook_secret"] = "${env:ARGUS_WEBHOOK_SECRET}"
    return {
        "company": {
            "name": "argus",
            "defaults": defaults,
            "sources": [],
        },
        "teams": [team],
    }


def _channels(channel: str, channel_id: str | None) -> list[dict[str, Any]]:
    if channel == "none":
        return []
    if channel == "cli":
        return [{"type": "cli", "role": "control", "channel_id": channel_id or "local"}]
    if channel == "fake":
        return [{"type": "fake", "role": "control", "channel_id": channel_id or "local"}]
    if channel == "slack":
        return [{
            "type": "slack",
            "role": "control",
            "channel_id": channel_id or "C_REPLACE_ME",
            "secret_ref": "${env:SLACK_BOT_TOKEN}",
            "config": {"signing_secret": "${env:SLACK_SIGNING_SECRET}"},
        }]
    if channel == "telegram":
        return [{
            "type": "telegram",
            "role": "control",
            "channel_id": channel_id or "12345",
            "secret_ref": "${env:TELEGRAM_BOT_TOKEN}",
        }]
    if channel == "whatsapp":
        return [{
            "type": "whatsapp",
            "role": "control",
            "channel_id": channel_id or "120363_REPLACE_ME@g.us",
            "secret_ref": "${env:ARGUS_WA_APIKEY}",
            "config": {
                "base_url": "${env:ARGUS_WA_URL}",
                "instance": "${env:ARGUS_WA_INSTANCE}",
            },
        }]
    raise ValueError(f"unsupported channel: {channel}")


def _merge_config(path: Path, new_doc: dict[str, Any], team_name: str) -> dict[str, Any]:
    current = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(current, dict):
        return new_doc
    current.setdefault("company", new_doc["company"])
    company = current.setdefault("company", {})
    company.setdefault("defaults", new_doc["company"].get("defaults", {}))
    company.setdefault("sources", new_doc["company"].get("sources", []))
    teams = [team for team in current.get("teams", []) if team.get("name") != team_name]
    teams.append(new_doc["teams"][0])
    current["teams"] = teams
    return current


def _dump_yaml(raw: dict[str, Any]) -> str:
    return yaml.safe_dump(raw, sort_keys=False, default_flow_style=False)


def _env_example(scan: ProjectScan, channel: str) -> str:
    keys: list[str] = ["ARGUS_DB_DSN", "ARGUS_RUN_ROOT"]
    if channel in {"slack", "telegram", "whatsapp"}:
        keys.append("ARGUS_WEBHOOK_SECRET")
    if channel == "slack":
        keys += ["SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET"]
    if channel == "telegram":
        keys += ["TELEGRAM_BOT_TOKEN"]
    if channel == "whatsapp":
        keys += ["ARGUS_WA_APIKEY", "ARGUS_WA_INSTANCE", "ARGUS_WA_URL"]
    for provider in scan.provider_hints:
        keys.extend(CONNECTOR_ENV.get(provider, ()))
    keys = sorted(dict.fromkeys(keys))
    return "".join(f"{key}=\n" for key in keys)


def _checklist(scan: ProjectScan, *, mode: str, team_name: str,
               config_path: Path, channel: str) -> str:
    lines = [
        f"# Argus Onboarding: {team_name}",
        "",
        f"- Mode: `{mode}`",
        f"- Repo: `{scan.path}`",
        f"- Config: `{config_path}`",
        f"- Channel: `{channel}`",
        "",
        "## Detected",
        "",
        f"- Docs: {', '.join(scan.docs) if scan.docs else 'none'}",
        f"- Package files: {', '.join(scan.package_files) if scan.package_files else 'none'}",
        f"- CI workflows: {', '.join(scan.ci_workflows) if scan.ci_workflows else 'none'}",
        f"- Vercel projects: {', '.join(scan.vercel_projects) if scan.vercel_projects else 'none'}",
        f"- Test command: `{scan.test_command}`" if scan.test_command else "- Test command: not detected",
        f"- GitHub repo: `{scan.github_repo}`" if scan.github_repo else "- GitHub repo: not detected",
        f"- Env keys detected: {', '.join(scan.env_keys) if scan.env_keys else 'none'}",
        f"- Provider hints: {', '.join(scan.provider_hints) if scan.provider_hints else 'none'}",
        "",
        "## Required Checks",
        "",
        "- Fill `argus.env.example.generated` with real values in a private env file.",
        "- Run `argus doctor --deep --live --json`.",
        "- Start `argus serve` and `argus up` as host jobs.",
        "- Run `argus go-live --mode " + mode + "`.",
        "",
        "## Connector Stubs",
        "",
    ]
    if not scan.provider_hints:
        lines.append("No connector hints detected.")
    else:
        for provider in scan.provider_hints:
            envs = ", ".join(CONNECTOR_ENV.get(provider, ()))
            note = CONNECTOR_NOTES.get(provider)
            suffix = f" {note}" if note else ""
            lines.append(
                f"- `{provider}`: add only after `{envs}` exists and dry-run passes."
                f"{suffix}"
            )
        require_flags = " ".join(
            f"--require-source-type {provider}" for provider in scan.provider_hints
        )
        team_require_flags = " ".join(
            f"--require-team-source-type {team_name}:{provider}"
            for provider in scan.provider_hints
        )
        each_team_require_flags = " ".join(
            f"--require-each-team-source-type {provider}"
            for provider in scan.provider_hints
        )
        lines += [
            "",
            "## Provider Gate",
            "",
            "If these provider hints are required for this install, keep them in the live gate:",
            "",
            "```bash",
            f"argus doctor --deep --live --json {require_flags}",
            f"argus go-live --mode {mode} {require_flags}",
            f"argus doctor --deep --live --json {team_require_flags}",
            f"argus go-live --mode {mode} {team_require_flags}",
            f"argus doctor --deep --live --json {each_team_require_flags}",
            f"argus go-live --mode {mode} {each_team_require_flags}",
            "```",
        ]
    lines += [
        "",
        "Secrets were not copied from `.env`. Only env key names were detected.",
        "",
    ]
    return "\n".join(lines)


def _env_keys(repo: Path) -> list[str]:
    keys: set[str] = set()
    for name in (".env.example", ".env", ".env.local", ".env.development", ".env.production"):
        path = repo / name
        if not path.exists() or not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            key = _env_key(line)
            if key:
                keys.add(key)
    return sorted(keys)


def _env_key(line: str) -> str | None:
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    if text.startswith("export "):
        text = text[7:].strip()
    if "=" not in text:
        return None
    key = text.split("=", 1)[0].strip()
    return key if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key) else None


def _vercel_projects(repo: Path) -> list[str]:
    projects: list[str] = []
    skip = {".git", "node_modules", ".next", "dist", "build"}
    paths: list[Path] = []
    for root, dirs, _files in os.walk(repo):
        dirs[:] = [item for item in dirs if item not in skip]
        candidate = Path(root) / ".vercel" / "project.json"
        if candidate.exists():
            paths.append(candidate)
    for path in sorted(paths):
        rel = path.relative_to(repo)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            projects.append(str(rel))
            continue
        project = data.get("projectName") or data.get("projectId") or "unknown"
        org = data.get("orgId") or "unknown"
        projects.append(f"{rel}: {project} ({org})")
    return projects


def _provider_hints(
    repo: Path,
    env_keys: set[str],
    *,
    vercel_projects: list[str] | None = None,
) -> list[str]:
    hints: set[str] = set()
    if (
        (repo / "vercel.json").exists()
        or (repo / ".vercel" / "project.json").exists()
        or vercel_projects
    ):
        hints.add("vercel")
    if (repo / "supabase").exists() or any(key.startswith("SUPABASE_") for key in env_keys):
        hints.add("supabase")
    if (repo / "firebase.json").exists() or (repo / ".firebaserc").exists():
        hints.add("firebase")
    if any(key.startswith("SENTRY_") for key in env_keys):
        hints.add("sentry")
    if "GITHUB_TOKEN" in env_keys:
        hints.add("github")
    if (repo / "fly.toml").exists():
        hints.add("fly")
    if any("POSTHOG" in key for key in env_keys):
        hints.add("posthog")
    if "DATABASE_URL" in env_keys or "POSTGRES_URL" in env_keys:
        hints.add("postgres")
    if "OPENAPI_URL" in env_keys:
        hints.add("openapi")
    if "ARGUS_WEBHOOK_SOURCE_URL" in env_keys:
        hints.add("webhook")
    if "ARGUS_UPTIME_URL" in env_keys:
        hints.add("uptime")
    return sorted(hints)


def _commands(repo: Path) -> tuple[str | None, str | None]:
    package = repo / "package.json"
    if package.exists():
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        scripts = data.get("scripts") if isinstance(data, dict) else {}
        if isinstance(scripts, dict):
            if (repo / "pnpm-lock.yaml").exists():
                runner, install = "pnpm", "pnpm install --frozen-lockfile"
            elif (repo / "yarn.lock").exists():
                runner, install = "yarn", "yarn install --frozen-lockfile"
            elif (repo / "package-lock.json").exists():
                runner, install = "npm", "npm ci"
            else:
                runner, install = "npm", "npm install"
            if "test" in scripts:
                return f"{runner} test", install
            if "lint" in scripts:
                return f"{runner} run lint", install
    if (repo / "pyproject.toml").exists() or (repo / "pytest.ini").exists():
        return "pytest -q", "python -m pip install -e ."
    if (repo / "go.mod").exists():
        return "go test ./...", None
    if (repo / "Cargo.toml").exists():
        return "cargo test", None
    return None, None


def _git(repo: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              text=True, check=False, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and out else None


def _github_repo(remote: str | None) -> str | None:
    if not remote:
        return None
    match = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?$", remote)
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def _base_branch(repo: Path) -> str:
    ref = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if ref and "/" in ref:
        return ref.split("/", 1)[1]
    for branch in ("main", "master"):
        if _git(repo, "rev-parse", "--verify", "--quiet", branch):
            return branch
    return "main"


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "project"
