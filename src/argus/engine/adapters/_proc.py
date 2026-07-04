"""Shared subprocess runner for CLI-driving adapters.

Mirrors the bash adapters' retry loop: a transient rate-limit / overload / 5xx
on stderr is retried with backoff (ARGUS_ENGINE_MAX_RETRIES, default 2;
ARGUS_ENGINE_RETRY_DELAY overrides the attempt*5s backoff); any other failure
returns None immediately so the adapter can fail closed with the outage code.
"""
import os
import queue
import re
import subprocess
import threading
import time
from typing import Callable, List, Optional

TRANSIENT = re.compile(
    r"rate limit|temporarily|overloaded|too many requests|\b429\b|\b50[0-9]\b|server error",
    re.IGNORECASE,
)
_LAST_STDERR = ""


def last_stderr() -> str:
    return _LAST_STDERR


def run_with_retries(
    argv: List[str], *, cwd: str, stdin_text: Optional[str] = None,
    env: Optional[dict] = None, progress_heartbeat: Optional[Callable[[], object]] = None
) -> Optional[str]:
    global _LAST_STDERR
    _LAST_STDERR = ""
    max_retries = int(os.environ.get("ARGUS_ENGINE_MAX_RETRIES", "2"))
    attempt = 0
    while True:
        idle_timeout = _idle_timeout()
        max_runtime = _max_runtime()
        kwargs: dict = {
            "cwd": cwd,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
        }
        if env is not None:
            kwargs["env"] = {**os.environ, **env}
        if stdin_text is not None:
            kwargs["stdin"] = subprocess.PIPE
        else:
            # Never inherit the caller's stdin: a child that reads stdin (cat,
            # codex exec -) must see EOF, not block an interactive terminal.
            kwargs["stdin"] = subprocess.DEVNULL
        try:
            proc = subprocess.Popen(argv, **kwargs)
        except (FileNotFoundError, PermissionError):
            # Parity with bash rc 127/126: a vanished or non-executable binary
            # is a hard failure, fail closed like any other non-transient error.
            _LAST_STDERR = "process could not be started"
            return None
        stdout, stderr, timed_out = _communicate_with_progress(
            proc,
            stdin_text=stdin_text,
            idle_timeout=idle_timeout,
            max_runtime=max_runtime,
            progress_heartbeat=progress_heartbeat,
        )
        if timed_out:
            _LAST_STDERR = _failure_detail(stderr, stdout, timed_out)
            return None
        if proc.returncode == 0:
            return stdout
        _LAST_STDERR = stderr or ""
        if attempt < max_retries and TRANSIENT.search(stderr or ""):
            attempt += 1
            delay = os.environ.get("ARGUS_ENGINE_RETRY_DELAY")
            time.sleep(float(delay) if delay is not None else attempt * 5)
            continue
        return None


def _communicate_with_progress(
    proc: subprocess.Popen,
    *,
    stdin_text: Optional[str],
    idle_timeout: Optional[float],
    max_runtime: Optional[float],
    progress_heartbeat: Optional[Callable[[], object]],
) -> tuple[str, str, str]:
    events: "queue.Queue[tuple[str, str]]" = queue.Queue()
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def reader(name: str, stream) -> None:
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                events.put((name, chunk))
        except (OSError, ValueError):
            # ValueError: the main thread closes the pipe after kill/timeout
            # while this reader is blocked in read() on the same file object.
            pass

    def writer() -> None:
        if proc.stdin is None or stdin_text is None:
            return
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    stdout_thread = threading.Thread(target=reader, args=("stdout", proc.stdout), daemon=True)
    stderr_thread = threading.Thread(target=reader, args=("stderr", proc.stderr), daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    threads = [stdout_thread, stderr_thread]
    if stdin_text is not None:
        stdin_thread = threading.Thread(target=writer, daemon=True)
        stdin_thread.start()
        threads.append(stdin_thread)

    started = time.monotonic()
    last_activity = started
    last_marker = object()
    poll_interval = _poll_interval()
    timeout_reason = ""
    while proc.poll() is None:
        try:
            name, chunk = events.get(timeout=poll_interval)
            if name == "stdout":
                stdout_chunks.append(chunk)
            else:
                stderr_chunks.append(chunk)
            last_activity = time.monotonic()
        except queue.Empty:
            pass

        now = time.monotonic()
        if progress_heartbeat is not None:
            marker = progress_heartbeat()
            if marker is not None and marker != last_marker:
                last_marker = marker
                last_activity = now
        if idle_timeout is not None and now - last_activity > idle_timeout:
            timeout_reason = f"process idle timed out after {idle_timeout:g}s"
            proc.kill()
            break
        if max_runtime is not None and now - started > max_runtime:
            timeout_reason = f"process runtime exceeded after {max_runtime:g}s"
            proc.kill()
            break

    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    for stream in (proc.stdout, proc.stderr, proc.stdin):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            pass
    for thread in threads:
        thread.join(timeout=0.5)

    while True:
        try:
            name, chunk = events.get_nowait()
        except queue.Empty:
            break
        if name == "stdout":
            stdout_chunks.append(chunk)
        else:
            stderr_chunks.append(chunk)
    return "".join(stdout_chunks), "".join(stderr_chunks), timeout_reason


def _idle_timeout() -> Optional[float]:
    raw = os.environ.get("ARGUS_ENGINE_IDLE_TIMEOUT") or os.environ.get("ARGUS_ENGINE_TIMEOUT")
    timeout = float(raw) if raw else 900.0
    return timeout if timeout > 0 else None


def _max_runtime() -> Optional[float]:
    raw = os.environ.get("ARGUS_ENGINE_MAX_RUNTIME")
    if not raw:
        return None
    runtime = float(raw)
    return runtime if runtime > 0 else None


def _poll_interval() -> float:
    return max(0.01, float(os.environ.get("ARGUS_ENGINE_PROGRESS_POLL_INTERVAL", "0.25")))


def _failure_detail(stderr: str, stdout: str, reason: str) -> str:
    snippets: list[str] = []
    for chunk in (stderr, stdout):
        text = (chunk or "").strip()
        if text:
            snippets.append(text[-1000:])
    snippets.append(reason)
    return "\n".join(snippets)
