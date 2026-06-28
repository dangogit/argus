"""MCP server: JSON-RPC dispatch (hermetic) + read-only tool calls (DB-backed)."""
from __future__ import annotations

import io
import json

from argus.v2.mcp import server


# --- protocol dispatch (no DB) ---

def test_initialize_returns_server_info():
    resp = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert resp["result"]["serverInfo"]["name"] == "argus"
    assert resp["result"]["protocolVersion"] == server.PROTOCOL_VERSION


def test_tools_list_exposes_read_tools():
    resp = server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in resp["result"]["tools"]}
    assert names == {"argus_status", "argus_alerts", "argus_lessons", "argus_proposals"}


def test_ping_returns_empty_result():
    resp = server.handle_request({"jsonrpc": "2.0", "id": 3, "method": "ping"})
    assert resp["result"] == {}


def test_notification_gets_no_response():
    assert server.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method_errors():
    resp = server.handle_request({"jsonrpc": "2.0", "id": 4, "method": "frobnicate"})
    assert resp["error"]["code"] == -32601


def test_unknown_tool_errors():
    resp = server.handle_request(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "no_such_tool", "arguments": {}}})
    assert resp["error"]["code"] == -32602


def test_serve_loop_responds_per_line():
    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    stdout = io.StringIO()
    server.serve(stdin, stdout)
    lines = [l for l in stdout.getvalue().splitlines() if l]
    assert len(lines) == 1  # initialize answered, notification not
    assert json.loads(lines[0])["id"] == 1


# --- tool calls (DB-backed via the conn fixture's ARGUS_DB_DSN) ---

def _call(name, args=None):
    return server.handle_request(
        {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
         "params": {"name": name, "arguments": args or {}}})


def test_status_tool_on_empty_db(conn):
    resp = _call("argus_status")
    text = resp["result"]["content"][0]["text"]
    assert "open_requests=0" in text and "alerts_24h=0" in text


def test_alerts_tool_returns_inserted_alert(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO alerts (severity, project, fingerprint, message, channel) "
                    "VALUES ('error','web','f1','disk full','log')")
    conn.commit()
    resp = _call("argus_alerts")
    assert "disk full" in resp["result"]["content"][0]["text"]


def test_non_object_request_rejected():
    for bad in (42, [1, 2], "str", None):
        resp = server.handle_request(bad)
        assert resp["error"]["code"] == -32600  # invalid request, did not crash


def test_serve_survives_non_object_frame():
    stdin = io.StringIO("42\n" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n")
    stdout = io.StringIO()
    server.serve(stdin, stdout)
    answered = [json.loads(l) for l in stdout.getvalue().splitlines() if l]
    assert any(r.get("id") == 1 and "result" in r for r in answered)  # loop survived 42
