"""codex adapter: drives `codex exec`.

Cost is estimated from pinned pricing, not exact upstream telemetry. Writes are
confined by the workspace-write sandbox.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from argus.engine import EngineOutageError, EngineResult, write_meta
from argus.engine.adapters._proc import last_stderr, run_with_retries


_TIMEOUT_MARKERS = ("process timed out", "timed out after", "timeout", "deadline_exceeded")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|ACCESS_KEY|PRIVATE_KEY)"
    r"[A-Z0-9_]*)=([^\s]+)",
    re.IGNORECASE,
)
_ENV_PATH_RE = re.compile(r"(?i)(/[^\s]*(?:\.env|env\.|env-)[^\s]*)")


@dataclass(frozen=True)
class _ProgressSnapshot:
    path: Path
    checkpoints: tuple[str, ...]


def _failure_detail(*, cwd: str | None = None, started_at: float | None = None) -> str:
    lines = [line.strip() for line in last_stderr().splitlines() if line.strip()]
    detail = (lines[-1] if lines else "no stderr")[:300]
    if cwd and started_at is not None and _looks_timeout(detail):
        progress = _latest_progress_snapshot(cwd, started_at)
        if progress:
            items = "\n".join(f"- {item}" for item in progress.checkpoints)
            return (
                f"{detail}\n"
                f"Codex session: {progress.path}\n"
                f"Recent checkpoints:\n{items}"
            )[:1600]
    return detail


def _looks_timeout(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _TIMEOUT_MARKERS)


def _latest_progress_snapshot(cwd: str, started_at: float, *, limit: int = 8) \
        -> _ProgressSnapshot | None:
    path = _latest_matching_session(cwd, started_at)
    if path is None:
        return None
    checkpoints = tuple(_read_agent_checkpoints(path, limit=limit))
    if checkpoints:
        return _ProgressSnapshot(path=path, checkpoints=checkpoints)
    return None


def _latest_matching_session(cwd: str, started_at: float) -> Path | None:
    root = _codex_sessions_root()
    if not root.is_dir():
        return None
    wanted = _resolve_path(cwd)
    candidates: list[tuple[float, Path]] = []
    for path in root.rglob("rollout-*.jsonl"):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime < started_at - 5:
            continue
        candidates.append((stat.st_mtime, path))
    for _, path in sorted(candidates, reverse=True)[:25]:
        if not _session_matches_cwd(path, wanted):
            continue
        return path
    return None


def _codex_progress_heartbeat(cwd: str, started_at: float):
    last_poll = 0.0
    last_marker = None
    poll_interval = max(
        0.05,
        float(os.environ.get("ARGUS_CODEX_TRANSCRIPT_POLL_INTERVAL", "5")),
    )

    def heartbeat():
        nonlocal last_poll, last_marker
        now = time.monotonic()
        if now - last_poll < poll_interval:
            return last_marker
        last_poll = now
        path = _latest_matching_session(cwd, started_at)
        if path is None:
            return last_marker
        try:
            stat = path.stat()
        except OSError:
            return last_marker
        last_marker = (str(path), stat.st_mtime_ns, stat.st_size)
        return last_marker

    return heartbeat


def _codex_sessions_root() -> Path:
    home = os.environ.get("CODEX_HOME")
    return (Path(home) if home else Path.home() / ".codex") / "sessions"


def _config_override_args(raw: str | None) -> list[str]:
    if not raw:
        return []
    args: list[str] = []
    for item in shlex.split(raw):
        args += ["-c", item]
    return args


def _resolve_path(path: str) -> Path:
    try:
        return Path(path).resolve()
    except OSError:
        return Path(path).absolute()


def _session_matches_cwd(path: Path, wanted: Path) -> bool:
    for obj in _iter_jsonl(path, max_lines=80):
        if obj.get("type") != "session_meta":
            continue
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        raw_cwd = payload.get("cwd")
        if not isinstance(raw_cwd, str) or not raw_cwd:
            return False
        return _resolve_path(raw_cwd) == wanted
    return False


def _read_agent_checkpoints(path: Path, *, limit: int) -> list[str]:
    seen: set[str] = set()
    checkpoints: list[str] = []
    for obj in _iter_jsonl(path):
        text = _agent_message_text(obj)
        if not text:
            continue
        cleaned = _clean_checkpoint(text)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        checkpoints.append(cleaned)
    return checkpoints[-limit:]


def _agent_message_text(obj: dict) -> str:
    if obj.get("type") != "event_msg":
        return ""
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    if payload.get("type") != "agent_message":
        return ""
    message = payload.get("message") or payload.get("text")
    return message if isinstance(message, str) else ""


def _clean_checkpoint(text: str) -> str:
    text = " ".join(text.split())
    text = _SECRET_ASSIGNMENT_RE.sub(r"\1=[REDACTED]", text)
    text = _ENV_PATH_RE.sub("[REDACTED_ENV_PATH]", text)
    return text[:237] + "..." if len(text) > 240 else text


def _iter_jsonl(path: Path, *, max_lines: int | None = None):
    try:
        with path.open("r", encoding="utf-8") as f:
            for idx, line in enumerate(f):
                if max_lines is not None and idx >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


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
    argv += _config_override_args(os.environ.get("ARGUS_CODEX_CONFIG_OVERRIDES"))

    started_at = time.time()
    if stdin_mode:
        out = run_with_retries(
            argv + ["-"],
            cwd=cwd,
            stdin_text=prompt,
            progress_heartbeat=_codex_progress_heartbeat(cwd, started_at),
        )
    else:
        out = run_with_retries(
            argv + [prompt],
            cwd=cwd,
            progress_heartbeat=_codex_progress_heartbeat(cwd, started_at),
        )
    if out is None:
        detail = _failure_detail(cwd=cwd, started_at=started_at)
        print(f"codex engine failed: {bin_name} exited non-zero: {detail}", file=sys.stderr)
        raise EngineOutageError(f"{bin_name}: {detail}")
    return EngineResult(text=out.rstrip("\n"), cost_source="estimated")
