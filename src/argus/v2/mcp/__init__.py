"""MCP (Model Context Protocol) client support.

Argus does not hand-roll an MCP client for the engine path: the Claude Code
engine is itself an MCP client. Argus validates operator-declared MCP servers
and renders them into the `--mcp-config` format Claude Code consumes. A live
protocol handshake for `argus doctor` is P1 (see docs/mcp-support.md); P0
validates config shape and resolvability, which catches the common
misconfigurations (typo'd command, missing binary, unset secret) echo-safe.
"""
