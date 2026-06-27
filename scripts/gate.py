#!/usr/bin/env python3
"""Argus acceptance gate for the pure v2 product."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    pytest = os.environ.get("PYTEST") or str(root / ".venv" / "bin" / "pytest")
    if not Path(pytest).exists() and not shutil.which(pytest):
        pytest = "pytest"
    cov_min = os.environ.get("ARGUS_COV_MIN", "80")
    env = {**os.environ, "ARGUS_GATE": "1"}
    return subprocess.run([
        pytest,
        "tests/python",
        "--ignore=tests/python/v2/live",
        "-p", "no:cacheprovider",
        "-W", "error",
        "--strict-markers",
        "--strict-config",
        "-ra",
        "--cov=argus.v2",
        "--cov-report=term-missing",
        f"--cov-fail-under={cov_min}",
    ], cwd=root, env=env).returncode


if __name__ == "__main__":
    sys.exit(main())
