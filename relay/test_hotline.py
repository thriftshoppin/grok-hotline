#!/usr/bin/env python3
"""End-to-end tests of the hotline. Never prints the bearer token."""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

DIR = Path(__file__).resolve().parent
TOKEN_PATH = DIR / "mcp.token"
HOTLINE_PATH = DIR / "HOTLINE.txt"
RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    flag = "PASS" if ok else "FAIL"
    print("  [%s] %s%s" % (flag, name, (" — " + detail) if detail else ""), flush=True)


def load_token():
    token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit("mcp.token is empty")
    return token


def public_mcp_from_hotline():
    if not HOTLINE_PATH.exists():
        return None
    text = HOTLINE_PATH.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("https://") and line.endswith("/mcp"):
            return line
    return None


def rpc(url, payload, token=None, session=None, timeout=45):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2024-11-05",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    if session:
        headers["Mcp-Session-Id"] = session
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            new_session = r.headers.get("Mcp-Session-Id") or session
            body = r.read()
            parsed = json.loads(body.decode("utf-8")) if body else None
            return r.status, parsed, new_session
    except urllib.error.HTTPError as e:
        body = e.read() if e.fp else b""
        try:
            parsed = json.loads(body.decode("utf-8")) if body else None
        except ValueError:
            parsed = body.decode("utf-8", "replace")[:300] if body else None
        return e.code, parsed, session
    except urllib.error.URLError as e:
        return 0, {"error": str(e.reason)}, session


def run_suite(url, token, label):
    print("Testing MCP at %s (%s)" % (url.split("?")[0], label), flush=True)
    before = len(RESULTS)

    status, body, _ = rpc(url, {"jsonrpc": "2.0", "id": 0, "method": "ping"}, token="totally-wrong-token", timeout=15)
    record("bad token rejected", status == 401, "HTTP %s" % status)

    status, body, session = rpc(
        url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "hotline-e2e", "version": "1.0"},
            },
        },
        token=token,
        timeout=45,
    )
    ok = status == 200 and isinstance(body, dict) and "result" in body
    name = None
    if ok:
        name = ((body.get("result") or {}).get("serverInfo") or {}).get("name")
    record("initialize", ok, "HTTP %s server=%s" % (status, name))
    if not ok:
        return

    status, body, session = rpc(
        url,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        token=token,
        session=session,
        timeout=20,
    )
    record("notifications/initialized", 200 <= status < 300, "HTTP %s" % status)

    status, body, session = rpc(
        url,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        token=token,
        session=session,
        timeout=30,
    )
    tools = []
    if isinstance(body, dict) and isinstance((body.get("result") or {}).get("tools"), list):
        tools = [t.get("name") for t in body["result"]["tools"]]
    has_ok = "sysinfo" in tools or "desktop_session_status" in tools
    no_blocked = "desktop_run" not in tools
    record("tools/list filtered", status == 200 and has_ok and no_blocked, "HTTP %s tools=%s" % (status, tools))

    if "sysinfo" in tools:
        call_name, call_args = "sysinfo", {}
    else:
        call_name, call_args = "desktop_session_status", {}
    status, body, session = rpc(
        url,
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": call_name, "arguments": call_args}},
        token=token,
        session=session,
        timeout=40,
    )
    ok = status == 200 and isinstance(body, dict) and "result" in body and "error" not in body
    record("allowed tools/call succeeds", ok, "HTTP %s tool=%s" % (status, call_name))

    status, body, session = rpc(
        url,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "desktop_run", "arguments": {"command": "whoami"}},
        },
        token=token,
        session=session,
        timeout=20,
    )
    err = body.get("error") if isinstance(body, dict) else None
    ok = status == 200 and isinstance(err, dict) and err.get("code") == -32001
    leaked = "ran" in json.dumps(body) if body is not None else False
    record("blocked tool refused", ok and not leaked, "HTTP %s error=%s leaked=%s" % (status, err is not None, leaked))
    _ = before


def main() -> int:
    token = load_token()
    public = public_mcp_from_hotline()
    local = os.environ.get("HOTLINE_MCP_URL") or "http://127.0.0.1:18765/mcp"

    # Always run local first (this box's cloudflared edge may be blocked).
    run_suite(local, token, "local")
    public_ok = False
    if public:
        status, _, _ = rpc(public, {"jsonrpc": "2.0", "id": 0, "method": "ping"}, token="x", timeout=12)
        if status == 0:
            record("public tunnel reachable", False, "connection failed (edge blocked or 1033)")
        else:
            # 401 means the tunnel reached the relay
            public_ok = status in (401, 200, 503)
            record("public tunnel reachable", public_ok, "HTTP %s" % status)
            if public_ok and status != 401:
                run_suite(public, token, "public")
            elif public_ok and status == 401:
                run_suite(public, token, "public")

    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n%s/%s passed" % (len(RESULTS) - len(failed), len(RESULTS)), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
