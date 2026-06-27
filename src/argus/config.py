"""Small scalar reader for engine defaults in argus.config.yaml.

This is deliberately not a full YAML library. It reads a scalar at "block.key"
(one nesting level) or a bare top-level "key". It strips inline comments, trims
whitespace, unquotes one matching layer of quotes, and treats null, ~, or empty
values as unset.
"""
import os
import re
from pathlib import Path
from typing import Optional


def repo_root() -> Path:
    # src/argus/config.py -> src/argus -> src -> repo root
    return Path(__file__).resolve().parents[2]


def config_path() -> Optional[Path]:
    explicit = os.environ.get("ARGUS_CONFIG")
    if explicit:
        return Path(explicit)
    candidate = repo_root() / "argus.config.yaml"
    if candidate.is_file():
        return candidate
    return None


def config_get(dotted: str) -> Optional[str]:
    if not dotted:
        raise ValueError("key required")
    cfg = config_path()
    if cfg is None or not cfg.is_file():
        return None

    if "." in dotted:
        block, key = dotted.split(".", 1)
    else:
        block, key = None, dotted

    val = None
    in_block = False
    for raw in cfg.read_text(encoding="utf-8").splitlines():
        if block is None:
            # Bare top-level scalar: line with no leading whitespace.
            if raw[:1].isspace():
                continue
            # Comment stripping is quote-blind by design. Keep this reader
            # intentionally small and reserve full YAML parsing for v2 config.
            line = re.sub(r"#.*", "", raw)
            m = re.match(r"^" + re.escape(key) + r":\s*(.*?)\s*$", line)
            if m:
                val = m.group(1)
                break
        else:
            # A non-indented, non-comment line opens or closes the block.
            if raw and not raw[0].isspace() and raw[0] != "#":
                in_block = raw.startswith(block + ":")
                continue
            if not in_block:
                continue
            line = re.sub(r"#.*", "", raw)
            m = re.match(r"^\s+" + re.escape(key) + r":\s*(.*?)\s*$", line)
            if m:
                val = m.group(1)
                break

    if val is None:
        return None
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        val = val[1:-1]
    if val in ("", "null", "~"):
        return None
    return val


def run_root() -> Path:
    """Run-root rule: ARGUS_RUN_ROOT env > run_root: in config > <repo>/run."""
    env = os.environ.get("ARGUS_RUN_ROOT")
    if env:
        return Path(env)
    cfg = config_get("run_root")
    if cfg:
        return Path(cfg).expanduser()
    return repo_root() / "run"
