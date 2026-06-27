"""Shared subprocess runner for CLI-driving adapters.

Mirrors the bash adapters' retry loop: a transient rate-limit / overload / 5xx
on stderr is retried with backoff (ARGUS_ENGINE_MAX_RETRIES, default 2;
ARGUS_ENGINE_RETRY_DELAY overrides the attempt*5s backoff); any other failure
returns None immediately so the adapter can fail closed with the outage code.
"""
import os
import re
import subprocess
import time
from typing import List, Optional

TRANSIENT = re.compile(
    r"rate limit|temporarily|overloaded|too many requests|\b429\b|\b50[0-9]\b|server error",
    re.IGNORECASE,
)
_LAST_STDERR = ""


def last_stderr() -> str:
    return _LAST_STDERR


def run_with_retries(
    argv: List[str], *, cwd: str, stdin_text: Optional[str] = None, env: Optional[dict] = None
) -> Optional[str]:
    global _LAST_STDERR
    _LAST_STDERR = ""
    max_retries = int(os.environ.get("ARGUS_ENGINE_MAX_RETRIES", "2"))
    attempt = 0
    while True:
        timeout = float(os.environ.get("ARGUS_ENGINE_TIMEOUT", "900"))
        kwargs: dict = {"cwd": cwd, "capture_output": True, "text": True, "timeout": timeout}
        if env is not None:
            kwargs["env"] = {**os.environ, **env}
        if stdin_text is not None:
            kwargs["input"] = stdin_text
        else:
            # Never inherit the caller's stdin: a child that reads stdin (cat,
            # codex exec -) must see EOF, not block an interactive terminal.
            kwargs["stdin"] = subprocess.DEVNULL
        try:
            proc = subprocess.run(argv, **kwargs)
        except subprocess.TimeoutExpired:
            _LAST_STDERR = f"process timed out after {timeout:g}s"
            return None
        except (FileNotFoundError, PermissionError):
            # Parity with bash rc 127/126: a vanished or non-executable binary
            # is a hard failure, fail closed like any other non-transient error.
            _LAST_STDERR = "process could not be started"
            return None
        if proc.returncode == 0:
            return proc.stdout
        _LAST_STDERR = proc.stderr or ""
        if attempt < max_retries and TRANSIENT.search(proc.stderr or ""):
            attempt += 1
            delay = os.environ.get("ARGUS_ENGINE_RETRY_DELAY")
            time.sleep(float(delay) if delay is not None else attempt * 5)
            continue
        return None
