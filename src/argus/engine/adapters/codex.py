"""codex adapter: drives `codex exec`.

Cost is estimated from pinned pricing, not exact upstream telemetry. Writes are
confined by the workspace-write sandbox.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

from argus.engine import EngineOutageError, EngineResult, write_meta
from argus.engine.adapters._proc import last_stderr, run_with_retries


def _failure_detail() -> str:
    lines = [line.strip() for line in last_stderr().splitlines() if line.strip()]
    return (lines[-1] if lines else "no stderr")[:300]


def _git_metadata_dirs(cwd: str) -> list[str]:
    """Return Git metadata dirs needed for commits from linked worktrees."""
    dirs: list[str] = []
    for flag in ("--git-dir", "--git-common-dir"):
        r = subprocess.run(["git", "rev-parse", flag], cwd=cwd, capture_output=True, text=True)
        if r.returncode != 0:
            continue
        raw = r.stdout.strip()
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = Path(cwd) / path
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path.absolute()
        if resolved.is_dir():
            item = str(resolved)
            if item not in dirs:
                dirs.append(item)
    return dirs


def run(prompt: str) -> EngineResult:
    bin_name = os.environ.get("ARGUS_CODEX_BIN", "codex")
    cwd = os.environ.get("ARGUS_AGENT_CWD") or os.getcwd()
    sandbox = os.environ.get("ARGUS_CODEX_SANDBOX", "workspace-write")
    stdin_mode = os.environ.get("ARGUS_CODEX_STDIN", "0") == "1"
    hermetic = os.environ.get("ARGUS_ENGINE_IGNORE_USER_CONFIG", "0") == "1"

    binpath = shutil.which(bin_name)
    if binpath is None:
        print(f"codex engine unavailable: {bin_name} not found", file=sys.stderr)
        raise EngineOutageError(bin_name)

    write_meta("estimated", "")

    argv = [binpath, "exec"]
    if hermetic:
        argv.append("--ignore-user-config")
    # Argus runs codex in controlled dirs: git worktrees for the dev team, plain
    # temp dirs for the conversational manager. codex exec refuses a non-git dir
    # without this flag, and it is harmless inside a git repo.
    argv.append("--skip-git-repo-check")
    argv += ["--sandbox", sandbox]
    if sandbox == "workspace-write":
        for path in _git_metadata_dirs(cwd):
            argv += ["--add-dir", path]
    # Opt-in network for the workspace-write sandbox (gh/git fetch+push). Writes
    # stay confined to the worktree; only the network egress opens. Gated by the
    # per-job snapshot flag, surfaced here as ARGUS_CODEX_NETWORK by the worker.
    if os.environ.get("ARGUS_CODEX_NETWORK") == "1" and sandbox == "workspace-write":
        argv += ["-c", "sandbox_workspace_write.network_access=true"]

    if stdin_mode:
        out = run_with_retries(argv + ["-"], cwd=cwd, stdin_text=prompt)
    else:
        out = run_with_retries(argv + [prompt], cwd=cwd)
    if out is None:
        detail = _failure_detail()
        print(f"codex engine failed: {bin_name} exited non-zero: {detail}", file=sys.stderr)
        raise EngineOutageError(f"{bin_name}: {detail}")
    return EngineResult(text=out.rstrip("\n"), cost_source="estimated")
