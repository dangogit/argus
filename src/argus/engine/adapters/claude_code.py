"""claude-code adapter: drives Claude Code in print mode with a tool allowlist."""
import os
import shutil
import sys

from argus.engine import EngineOutageError, EngineResult, write_meta
from argus.engine.adapters._proc import last_stderr, run_with_retries


def _failure_detail() -> str:
    lines = [line.strip() for line in last_stderr().splitlines() if line.strip()]
    return (lines[-1] if lines else "no stderr")[:300]


def run(prompt: str) -> EngineResult:
    bin_name = os.environ.get("ARGUS_CLAUDE_BIN", "claude")
    # ${VAR-default} semantics: set-but-empty is honored as empty.
    tools = os.environ["ARGUS_CLAUDE_TOOLS"] if "ARGUS_CLAUDE_TOOLS" in os.environ else "Read,Grep,Glob"
    cwd = os.environ.get("ARGUS_AGENT_CWD") or os.getcwd()
    perm = os.environ.get("ARGUS_CLAUDE_PERMISSION_MODE", "default")
    hermetic = os.environ.get("ARGUS_ENGINE_IGNORE_USER_CONFIG", "0") == "1"

    binpath = shutil.which(bin_name)
    if binpath is None:
        print(f"claude-code engine unavailable: {bin_name} not found", file=sys.stderr)
        raise EngineOutageError(bin_name)

    write_meta("unpriced", "")

    argv = [binpath, "--print"]
    if hermetic:
        argv.append("--strict-mcp-config")
    # Operator-configured MCP servers (rendered by argus.v2.mcp.config.materialize
    # and passed via this env var) let the agent use external MCP tools.
    mcp_config = os.environ.get("ARGUS_CLAUDE_MCP_CONFIG")
    if mcp_config:
        argv += ["--mcp-config", mcp_config]
    allowed_tools = os.environ.get("ARGUS_CLAUDE_ALLOWED_TOOLS")
    if allowed_tools:
        argv += ["--allowedTools", allowed_tools]
    argv += ["--permission-mode", perm, "--tools", tools]

    out = run_with_retries(argv, cwd=cwd, stdin_text=prompt)
    if out is None:
        detail = _failure_detail()
        print(f"claude-code engine failed: {bin_name} exited non-zero: {detail}", file=sys.stderr)
        raise EngineOutageError(f"{bin_name}: {detail}")
    return EngineResult(text=out.rstrip("\n"), cost_source="unpriced")
