#!/usr/bin/env python3
"""Minimal stdio MCP server used to test the hotline without a real Eden."""

from __future__ import annotations

import json
import sys


def read_msg():
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    line = line.decode("utf-8", "replace").strip()
    if not line:
        return read_msg()
    return json.loads(line)


def write_msg(msg: dict) -> None:
    sys.stdout.buffer.write((json.dumps(msg, ensure_ascii=True) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


TOOLS = [
    {
        "name": "desktop_session_status",
        "description": "Session status (mock)",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "desktop_windows_list",
        "description": "Window list (mock)",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "desktop_run",
        "description": "Run a command (should be blocked by the hotline)",
        "inputSchema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
]


def handle(msg: dict) -> dict | None:
    method = msg.get("method")
    req_id = msg.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": (msg.get("params") or {}).get("protocolVersion") or "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock-eden", "version": "1.0"},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        if name == "desktop_session_status":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps({
                        "ok": True,
                        "session": "console",
                        "locked": False,
                        "mock": True,
                    })}]
                },
            }
        if name == "desktop_windows_list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps({
                        "windows": [{"title": "Mock Window", "hwnd": 42}],
                    })}]
                },
            }
        # Mock will happily "run" desktop_run so the allowlist, not the mock, must block it.
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps({"ok": True, "ran": name})}]
            },
        }
    if "id" in msg:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unknown method {method}"}}
    return None


def main() -> None:
    # Line-buffered-ish; write unbuffered already via buffer.flush
    while True:
        try:
            msg = read_msg()
        except Exception as e:
            sys.stderr.write(f"mock_eden parse error: {e}\n")
            sys.stderr.flush()
            continue
        if msg is None:
            break
        reply = handle(msg)
        if reply is not None:
            write_msg(reply)


if __name__ == "__main__":
    main()
