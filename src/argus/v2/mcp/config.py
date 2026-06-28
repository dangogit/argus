"""Validate MCP server config and render it for the Claude Code engine."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from urllib.parse import urlparse


def validate_server(server) -> list[str]:
    """Return a list of human-readable problems with one server, or [] if it is
    usable. Checks resolvability (PATH, url shape, env presence) that pydantic
    cannot - used by `argus doctor`. Pure except for PATH/env reads."""
    errors: list[str] = []
    if not server.name:
        errors.append("mcp server has no name")
    if server.transport == "stdio":
        if not server.command:
            errors.append(f"{server.name}: stdio server needs a command")
        elif shutil.which(server.command) is None:
            errors.append(f"{server.name}: command not found on PATH: {server.command}")
    elif server.transport == "http":
        if not server.url:
            errors.append(f"{server.name}: http server needs a url")
        else:
            parsed = urlparse(server.url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                errors.append(f"{server.name}: invalid url: {server.url}")
    for var in server.env:
        if var not in os.environ:
            errors.append(f"{server.name}: env var not set: {var}")
    return errors


def validate(servers) -> list[str]:
    """Flat list of problems across all servers. [] means all usable."""
    problems: list[str] = []
    for s in servers:
        problems.extend(validate_server(s))
    return problems


def render_claude_config(servers) -> dict:
    """Build the `mcpServers` object Claude Code's --mcp-config expects. Env var
    values are resolved here (the engine needs them) into a file that is written
    to a private run dir, never back into YAML."""
    out: dict = {}
    for s in servers:
        if s.transport == "stdio":
            entry: dict = {"command": s.command, "args": list(s.args)}
            if s.env:
                entry["env"] = {v: os.environ.get(v, "") for v in s.env}
        else:
            entry = {"type": "http", "url": s.url}
        out[s.name] = entry
    return {"mcpServers": out}


def materialize(servers, dest_dir: str | os.PathLike) -> str | None:
    """Write the rendered Claude MCP config to dest_dir and return its path, or
    None when no servers are configured (so callers add no flag)."""
    if not servers:
        return None
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / "mcp.json"
    path.write_text(json.dumps(render_claude_config(servers), indent=2), encoding="utf-8")
    return str(path)
