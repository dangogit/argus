"""Shared run-agent-with-fallback helpers for v2 callers.

Several callers (assistant memory, advisor tick, support cycle) each run a
prompt through an engine and fall back to a secondary engine on
EngineOutageError. This module factors out the two repeated pieces:

- tool_less_env: a context manager that runs the agent with tool access
  disabled and a scratch cwd, used by callers that must not let the model
  touch the real filesystem or repo tools.
- run_with_fallback: try the primary engine, and on EngineOutageError only,
  retry once with the fallback engine.

Callers pass their own run_agent reference (usually argus.engine.run_agent
re-exported at module scope) so tests can monkeypatch it per module.
"""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from typing import Callable

from argus.engine import EngineOutageError

RunAgent = Callable[[str, str], object]


def run_with_fallback(run_agent: RunAgent, engine: str, fallback: str | None,
                      prompt: str) -> str:
    """Run prompt on engine; on EngineOutageError only, retry once on fallback.

    Returns the result text. Propagates EngineOutageError if the fallback is
    unset, equal to the primary engine, or also outages.
    """
    try:
        return run_agent(engine, prompt).text
    except EngineOutageError:
        if fallback and fallback != engine:
            return run_agent(fallback, prompt).text
        raise


@contextmanager
def tool_less_env(*, prefix: str, timeout: str | None = None):
    """Run a block with tool access disabled and a scratch cwd.

    Sets ARGUS_CLAUDE_TOOLS to empty, ARGUS_CODEX_SANDBOX to read-only, and
    ARGUS_AGENT_CWD to a fresh temp directory (prefixed with `prefix`) for the
    duration of the block, restoring the previous values on exit.

    If `timeout` is given, ARGUS_ENGINE_TIMEOUT is set via setdefault
    semantics (only applied if not already set by an outer caller) to match
    the one call site (advisor) that pins a timeout this way.
    """
    keys = ["ARGUS_CLAUDE_TOOLS", "ARGUS_CODEX_SANDBOX", "ARGUS_AGENT_CWD"]
    if timeout is not None:
        keys.append("ARGUS_ENGINE_TIMEOUT")
    previous = {key: os.environ.get(key) for key in keys}
    with tempfile.TemporaryDirectory(prefix=prefix) as cwd:
        os.environ["ARGUS_CLAUDE_TOOLS"] = ""
        os.environ["ARGUS_CODEX_SANDBOX"] = "read-only"
        os.environ["ARGUS_AGENT_CWD"] = cwd
        if timeout is not None:
            os.environ.setdefault("ARGUS_ENGINE_TIMEOUT", timeout)
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
