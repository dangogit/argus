"""MCP client config: validation + render for the Claude Code engine. Pure,
no DB, no live engine (echo-safe)."""
from __future__ import annotations

import json
from pathlib import Path

from argus.v2.config import loader
from argus.v2.config.schema import McpServer
from argus.v2.mcp import config as mcp


def test_valid_stdio_server_has_no_errors():
    s = McpServer(name="fs", transport="stdio", command="sh", args=["-c", "true"])
    assert mcp.validate_server(s) == []


def test_stdio_missing_command_errors():
    s = McpServer(name="x", transport="stdio", command=None)
    errs = mcp.validate_server(s)
    assert any("needs a command" in e for e in errs)


def test_stdio_command_not_on_path_errors():
    s = McpServer(name="x", transport="stdio", command="definitely-not-a-real-binary-xyz")
    assert any("not found on PATH" in e for e in mcp.validate_server(s))


def test_http_bad_url_errors():
    s = McpServer(name="x", transport="http", url="not-a-url")
    assert any("invalid url" in e for e in mcp.validate_server(s))


def test_http_valid_url_ok():
    s = McpServer(name="x", transport="http", url="https://mcp.example.com/sse")
    assert mcp.validate_server(s) == []


def test_missing_env_var_errors(monkeypatch):
    monkeypatch.delenv("MCP_SECRET_XYZ", raising=False)
    s = McpServer(name="x", transport="stdio", command="sh", env=["MCP_SECRET_XYZ"])
    assert any("env var not set: MCP_SECRET_XYZ" in e for e in mcp.validate_server(s))


def test_render_resolves_env_into_stdio_entry(monkeypatch):
    monkeypatch.setenv("MCP_TOKEN", "s3cr3t")
    s = McpServer(name="gh", transport="stdio", command="sh", args=["-c", "x"], env=["MCP_TOKEN"])
    rendered = mcp.render_claude_config([s])
    assert rendered == {"mcpServers": {"gh": {
        "command": "sh", "args": ["-c", "x"], "env": {"MCP_TOKEN": "s3cr3t"}}}}


def test_render_http_entry():
    s = McpServer(name="web", transport="http", url="https://mcp.example.com")
    assert mcp.render_claude_config([s]) == {
        "mcpServers": {"web": {"type": "http", "url": "https://mcp.example.com"}}}


def test_materialize_none_when_empty(tmp_path):
    assert mcp.materialize([], tmp_path) is None


def test_materialize_writes_file(tmp_path):
    s = McpServer(name="fs", transport="stdio", command="sh")
    path = mcp.materialize([s], tmp_path)
    assert path is not None
    data = json.loads(Path(path).read_text())
    assert "fs" in data["mcpServers"]


def test_doctor_mcp_check_flags_bad_server(tmp_path):
    from argus.v2 import opscheck
    y = tmp_path / "a.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n  - name: dev\n    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "mcp:\n  servers:\n"
        "    - { name: good, transport: stdio, command: sh }\n"
        "    - { name: bad, transport: stdio, command: definitely-not-real-xyz }\n")
    cfg = loader.load(y)
    checks = opscheck._mcp_checks(cfg)
    by_name = {c.name: c for c in checks}
    assert by_name["good"].status == "ok"
    assert by_name["bad"].status == "blocked"


def test_config_loads_mcp_block(tmp_path):
    y = tmp_path / "a.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n  - name: dev\n    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "mcp:\n  servers:\n"
        "    - { name: fs, transport: stdio, command: sh, args: ['-c','true'] }\n")
    cfg = loader.load(y)
    assert len(cfg.mcp.servers) == 1
    assert cfg.mcp.servers[0].name == "fs"
