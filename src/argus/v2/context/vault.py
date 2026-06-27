"""Markdown vault writer compatible with the legacy context vault."""
from __future__ import annotations

import json
import os
import random
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def apply_fact(vault: Path | str, project: str, key: str, value: str, iso: str) -> str:
    root = Path(os.path.expandvars(str(vault))).expanduser()
    value_max = _env_int("ARGUS_CONTEXT_VALUE_MAX", 500)
    window = _env_int("ARGUS_CONTEXT_DEFER_WINDOW_SEC", 600)
    k = _coerce(key, 0)
    v = _coerce(value, value_max)
    if not k or not v:
        return "noop"

    slug = _slug(project)
    file = _note_path(root, project, slug)
    relpath = f"wiki/projects/{slug}.md"
    exists = file.exists()
    md = file.read_text(encoding="utf-8") if exists else _empty_note(slug)
    lines = md.splitlines()
    prefix = f"- {k}: "
    existing_idx = next((i for i, line in enumerate(lines) if line.startswith(prefix)), None)
    old_value = ""
    if existing_idx is not None:
        old_value = lines[existing_idx][len(prefix):].strip()
        if old_value == v:
            return "noop"

    if exists and _recent_human_edit(root, relpath, file, window):
        return "deferred"

    facts_idx = next((i for i, line in enumerate(lines) if line.strip() == "## Facts"), None)
    if facts_idx is None:
        while lines and not lines[-1].strip():
            lines.pop()
        lines.extend(["", "## Facts"])
        facts_idx = len(lines) - 1

    target = f"- {k}: {v}"
    if existing_idx is None:
        lines.insert(facts_idx + 1, target)
    else:
        lines[existing_idx] = target
        lines.insert(existing_idx + 1, f"  - superseded {iso}: {old_value}")

    updated = False
    for i, line in enumerate(lines):
        if line.startswith("updated:"):
            lines[i] = f"updated: {iso}"
            updated = True
    if not updated:
        if lines and lines[0] == "---":
            lines.insert(1, f"updated: {iso}")
        else:
            lines = ["---", "type: project", f"project: {slug}", f"updated: {iso}", "---", "", *lines]

    _atomic_write(file, "\n".join(lines) + "\n")
    _ledger_set(root, relpath, int(file.stat().st_mtime))
    log = root / "log.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(f"{iso} {slug} {k} := {v}\n")
    return "written"


def _slug(project: str) -> str:
    value = (project or "").strip().lower()
    value = re.sub(r"[^a-z0-9_-]+", "-", value)
    value = value.strip("-")
    return value or "general"


def _note_path(root: Path, project: str, slug: str) -> Path:
    if "/" in (project or "") or ".." in (project or ""):
        raise ValueError(f"vault: refusing unsafe project: {project}")
    if "/" in slug or ".." in slug:
        raise ValueError(f"vault: unsafe slug derived from project: {project}")
    return root / "wiki" / "projects" / f"{slug}.md"


def _coerce(text: str, max_chars: int) -> str:
    value = re.sub(r"[\r\n]+", " ", str(text or ""))
    value = re.sub(r"\s{2,}", " ", value).strip()
    if max_chars > 0 and len(value) > max_chars:
        value = value[:max_chars] + " ..."
    return value


def _empty_note(slug: str) -> str:
    return f"---\ntype: project\nproject: {slug}\nupdated: \n---\n\n# {slug}\n\n## Facts\n"


def _ledger_path(root: Path) -> Path:
    return root / ".job-state.json"


def _ledger_get(root: Path, relpath: str) -> int | None:
    path = _ledger_path(root)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get(relpath, {}).get("jobMtimeMs")
        return int(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _ledger_set(root: Path, relpath: str, mtime: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = _ledger_path(root)
    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except json.JSONDecodeError:
            data = {}
    data[relpath] = {"jobMtimeMs": int(mtime)}
    _atomic_write(path, json.dumps(data, separators=(",", ":")) + "\n")


def _recent_human_edit(root: Path, relpath: str, file: Path, window: int) -> bool:
    recorded = _ledger_get(root, relpath)
    if recorded is None:
        return False
    on_disk = int(file.stat().st_mtime)
    now = int(datetime.now(timezone.utc).timestamp())
    return on_disk > recorded + 2 and now - on_disk < window


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = f".{random.randint(0, 999999):06d}.tmp"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=f".{path.name}.", suffix=suffix,
                                     delete=False) as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        return int(raw)
    except ValueError:
        return default
