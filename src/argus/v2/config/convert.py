"""Convert legacy Argus YAML into the v2 typed config shape."""
from __future__ import annotations

from pathlib import Path

import yaml


def convert_file(path: Path, *, project_dirs: list[Path] | None = None) -> dict:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("legacy config must be a mapping")
    return convert(raw, project_dirs=project_dirs or [])


def convert(raw: dict, *, project_dirs: list[Path]) -> dict:
    engine = _engine(raw)
    teams = [_team_from_project(project) for project in _projects(project_dirs)]
    if not teams:
        teams = [_default_team()]
    return {
        "company": {
            "name": str(raw.get("company", {}).get("name") if isinstance(raw.get("company"), dict) else "argus"),
            "defaults": {
                "engine": {"engine": engine},
                "autonomy": {
                    "reversible_internal": "auto",
                    "personal_outward": "approval",
                    "irreversible_outward": "approval",
                },
            },
            "sources": [],
        },
        "teams": teams,
    }


def dumps(data: dict) -> str:
    return yaml.safe_dump(data, sort_keys=False)


def _engine(raw: dict) -> str:
    engine = raw.get("engine")
    if isinstance(engine, dict):
        value = str(engine.get("default") or "codex")
    else:
        value = str(engine or "codex")
    if value not in {"echo", "scripted", "codex", "claude-code", "hermes"}:
        return "codex"
    return value


def _projects(project_dirs: list[Path]) -> list[dict]:
    projects: list[dict] = []
    for root in project_dirs:
        if not root.exists():
            continue
        for path in sorted(root.glob("*/project.yaml")):
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                raw.setdefault("_dir_name", path.parent.name)
                projects.append(raw)
    return projects


def _team_from_project(project: dict) -> dict:
    name = str(project.get("name") or project.get("_dir_name") or "project")
    repo = project.get("repo")
    autofix = project.get("autofix") if isinstance(project.get("autofix"), dict) else {}
    pm = project.get("pm") if isinstance(project.get("pm"), dict) else {}
    return {
        "name": name,
        "project": {
            "repo": str(repo or "."),
            "base_branch": str(project.get("base_branch") or "main"),
            "work_branch_prefix": f"argus/{name}",
            "autofix": {
                "mode": str(autofix.get("mode") or "propose-only"),
                "draft": bool(autofix.get("draft") or False),
                "force_draft_on_fail": bool(autofix.get("force_draft_on_fail", True)),
            },
            "pm": {
                "daily_limit": int(pm.get("daily_limit") or 3),
                "max_rework_attempts": pm.get("max_rework_attempts"),
            },
        },
        "roles": [
            {"name": "researcher", "kind": "front", "prompt": "Research and scope the finding."},
            {"name": "developer", "kind": "builder", "prompt":
             "Implement the fix. End with ARGUS_RESULT: "
             "{\"ready\": true, \"summary\": \"<1-2 plain-language sentences "
             "for the owner: what was wrong and what you changed>\"} when you "
             "edited code, or {\"ready\": false, \"analysis\": \"<why no change "
             "was warranted>\"} otherwise."},
            {"name": "qa", "kind": "judge", "prompt":
             "Run checks and judge the result. QA-sensitive work cannot close "
             "unless the transcript documents the access path, item "
             "disposition, verification coverage, and unresolved follow-up "
             "condition. "
             "Before failing, classify the "
             "failure as code regression, environment blocker, expected "
             "cancellation, stale status, or unknown."},
            {"name": "senior", "kind": "judge", "prompt":
             "Review the change before PR. QA-sensitive work cannot close "
             "unless the transcript documents the access path, item "
             "disposition, verification coverage, and unresolved follow-up "
             "condition. "
             "Before a failing PR summary, "
             "classify the failure as code regression, environment blocker, "
             "expected cancellation, stale status, or unknown."},
        ],
        "pipeline": {"stages": ["researcher", "developer", "qa", "senior"], "max_iters": 2},
        "channels": [{"type": "cli", "role": "control", "channel_id": "local"}],
    }


def _default_team() -> dict:
    return {
        "name": "dev",
        "roles": [{"name": "developer", "kind": "builder", "prompt": "Implement the requested change."}],
        "pipeline": {"stages": ["developer"]},
        "channels": [{"type": "cli", "role": "control", "channel_id": "local"}],
    }
