"""Small .env loader for the v2 runtime.

launchd does not source shell files. This module lets v2 services load the
same operator-owned env files directly from Python, without putting secrets in
plists or keeping a shell wrapper in the hot path.
"""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Iterable

_ASSIGN = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def split_env_files(raw: str | None) -> list[str]:
    """Split ARGUS_ENV_FILES. Accept os.pathsep and comma for operator comfort."""
    if not raw:
        return []
    out: list[str] = []
    for chunk in raw.split(os.pathsep):
        for item in chunk.split(","):
            item = item.strip()
            if item:
                out.append(item)
    return out


def load_from_environment(*, override: bool = False) -> list[Path]:
    """Load files named by ARGUS_ENV_FILE and ARGUS_ENV_FILES.

    Existing process env wins by default. This keeps launchd overrides and test
    monkeypatches authoritative while still loading secrets and runtime defaults.
    """
    paths: list[str] = []
    single = os.environ.get("ARGUS_ENV_FILE")
    if single:
        paths.append(single)
    paths.extend(split_env_files(os.environ.get("ARGUS_ENV_FILES")))
    return load_env_files(paths, override=override)


def load_env_files(paths: Iterable[str | Path], *, override: bool = False) -> list[Path]:
    loaded: list[Path] = []
    for pathish in paths:
        path = Path(pathish).expanduser()
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            parsed = parse_assignment(raw)
            if parsed is None:
                continue
            key, value = parsed
            if override or key not in os.environ:
                os.environ[key] = value
        loaded.append(path)
    _apply_aliases()
    return loaded


def parse_assignment(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    match = _ASSIGN.match(stripped)
    if not match:
        return None
    key, value = match.group(1), match.group(2).strip()
    return key, _expand_home(_parse_value(value))


def _parse_value(value: str) -> str:
    if not value:
        return ""
    try:
        parts = shlex.split(value, comments=True, posix=True)
    except ValueError:
        return value.strip().strip("'\"")
    if not parts:
        return ""
    return parts[0]


def _expand_home(value: str) -> str:
    home = os.environ.get("HOME", str(Path.home()))
    return value.replace("${HOME}", home).replace("$HOME", home).replace("~", home, 1)


def _apply_aliases() -> None:
    if "GH_TOKEN" not in os.environ and os.environ.get("GITHUB_TOKEN"):
        os.environ["GH_TOKEN"] = os.environ["GITHUB_TOKEN"]
    if "ARGUS_WA_APIKEY" not in os.environ and os.environ.get("EVOLUTION_API_KEY"):
        os.environ["ARGUS_WA_APIKEY"] = os.environ["EVOLUTION_API_KEY"]
    if "ARGUS_WA_URL" not in os.environ:
        for key in ("EVOLUTION_API_URL", "EVOLUTION_URL"):
            if os.environ.get(key):
                os.environ["ARGUS_WA_URL"] = os.environ[key]
                break
    if "ARGUS_WA_INSTANCE" not in os.environ and os.environ.get("EVOLUTION_INSTANCE"):
        os.environ["ARGUS_WA_INSTANCE"] = os.environ["EVOLUTION_INSTANCE"]
