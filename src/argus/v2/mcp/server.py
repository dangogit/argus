"""Minimal MCP server exposing Argus read-only state over stdio.

stdio transport = newline-delimited JSON-RPC 2.0 (one object per line). The
protocol surface Argus needs is small (initialize / tools/list / tools/call /
ping), so this is hand-rolled - no SDK dependency. Tools are READ-ONLY by
design: status, alerts, lessons, proposals. It never mutates or executes;
gated action tools (propose/approve) are P2 and must route through the existing
approval gate (see docs/mcp-support.md).
"""
from __future__ import annotations

import json
import sys

from argus.v2.db import pool

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "argus", "version": "0.1.0"}


def _tool_status(conn, args) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM requests WHERE status='open'")
        open_req = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM jobs WHERE status='pending'")
        pending = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM actions WHERE status='awaiting_approval'")
        waiting = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM alerts WHERE ts > now() - interval '24 hours'")
        alerts = cur.fetchone()[0]
    return (f"open_requests={open_req} pending_jobs={pending} "
            f"awaiting_approval={waiting} alerts_24h={alerts}")


def _tool_alerts(conn, args) -> str:
    limit = max(1, min(int(args.get("limit", 20)), 100))
    with conn.cursor() as cur:
        cur.execute("SELECT severity, project, message FROM alerts ORDER BY ts DESC LIMIT %s",
                    (limit,))
        rows = cur.fetchall()
    return "\n".join(f"[{s}] {p}: {m}" for s, p, m in rows) if rows else "no alerts"


def _tool_lessons(conn, args) -> str:
    team = args.get("team")
    with conn.cursor() as cur:
        if team:
            cur.execute("SELECT team_id, statement FROM retro_backlog "
                        "WHERE type='lesson' AND team_id=%s ORDER BY priority DESC LIMIT 50",
                        (team,))
        else:
            cur.execute("SELECT team_id, statement FROM retro_backlog "
                        "WHERE type='lesson' ORDER BY priority DESC LIMIT 50")
        rows = cur.fetchall()
    return "\n".join(f"{t}: {s}" for t, s in rows) if rows else "no lessons"


def _tool_proposals(conn, args) -> str:
    limit = max(1, min(int(args.get("limit", 20)), 100))
    with conn.cursor() as cur:
        cur.execute("SELECT type, status, COALESCE(destination_ref,'') FROM actions "
                    "WHERE status IN ('proposed','awaiting_approval') LIMIT %s", (limit,))
        rows = cur.fetchall()
    return "\n".join(f"{t} [{st}] {ref}" for t, st, ref in rows) if rows else "no open proposals"


TOOLS = {
    "argus_status": {
        "description": "Current Argus state: open requests, pending jobs, approvals waiting, 24h alerts.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _tool_status,
    },
    "argus_alerts": {
        "description": "Recent production alerts Argus has surfaced.",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}},
        "handler": _tool_alerts,
    },
    "argus_lessons": {
        "description": "Retro lessons Argus has learned, optionally filtered by team.",
        "inputSchema": {"type": "object", "properties": {"team": {"type": "string"}}},
        "handler": _tool_lessons,
    },
    "argus_proposals": {
        "description": "Open proposed/awaiting-approval actions (e.g. draft PRs).",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}},
        "handler": _tool_proposals,
    },
}


def _result(mid, result) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def handle_request(req: dict, *, connect=pool.connect) -> dict | None:
    """Dispatch one JSON-RPC request. Returns the response dict, or None for a
    notification (no id / no reply expected). Pure except for the tool handlers'
    DB reads via `connect`, which is injectable for tests."""
    mid = req.get("id")
    method = req.get("method")
    if method == "initialize":
        return _result(mid, {"protocolVersion": PROTOCOL_VERSION,
                             "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO})
    if method == "ping":
        return _result(mid, {})
    if method is not None and method.startswith("notifications/"):
        return None  # notification: no response
    if method == "tools/list":
        tools = [{"name": n, "description": t["description"], "inputSchema": t["inputSchema"]}
                 for n, t in TOOLS.items()]
        return _result(mid, {"tools": tools})
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = TOOLS.get(name)
        if tool is None:
            return _error(mid, -32602, f"unknown tool: {name}")
        conn = connect()
        try:
            text = tool["handler"](conn, args)
            return _result(mid, {"content": [{"type": "text", "text": text}]})
        except Exception as exc:  # surface as a tool error, not a transport crash
            return _result(mid, {"content": [{"type": "text", "text": f"error: {exc}"}],
                                 "isError": True})
        finally:
            conn.close()
    return _error(mid, -32601, f"method not found: {method}")


def serve(stdin=None, stdout=None) -> int:
    """Read newline-delimited JSON-RPC from stdin, write responses to stdout."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue  # ignore garbage frames
        resp = handle_request(req)
        if resp is not None:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()
    return 0
