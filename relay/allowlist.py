"""Tool allowlists shared by the box-side relay.

The deck script (grok_hotline.py) carries an identical copy so it stays a
single file with no extra imports on Windows.
"""

# Used when the deck proxies the Eden desktop bridge.
EDEN_TOOLS = frozenset({
    "desktop_session_status",
    "desktop_monitors_list",
    "desktop_screenshot",
    "desktop_zoom",
    "desktop_windows_list",
    "desktop_ui_tree",
    "desktop_find",
    "desktop_audio_level",
    "desktop_audio_session",
    "desktop_audio_analyze",
})

# Built-in scan tools when Eden is not installed on the deck.
BUILTIN_TOOLS = frozenset({
    "sysinfo",
    "processes",
    "disks",
    "network",
    "list_dir",
    "read_file",
    "screenshot",
})

ALLOWED_TOOLS = EDEN_TOOLS | BUILTIN_TOOLS

AUDIO_SESSION_ACTIONS = frozenset({"status", "check"})


def is_allowed_tool_name(name):
    return str(name or "") in ALLOWED_TOOLS


def is_allowed_call(name, arguments=None):
    name = str(name or "")
    if name not in ALLOWED_TOOLS:
        return False
    if name == "desktop_audio_session":
        action = ""
        if isinstance(arguments, dict):
            action = arguments.get("action", "")
        return str(action).lower() in AUDIO_SESSION_ACTIONS
    return True


def filter_tools_list_result(result):
    """Strip disallowed tools from a tools/list JSON-RPC result object."""
    if not isinstance(result, dict):
        return result
    tools = result.get("tools")
    if not isinstance(tools, list):
        return result
    out = dict(result)
    out["tools"] = [
        t for t in tools
        if isinstance(t, dict) and is_allowed_tool_name(t.get("name"))
    ]
    return out


def refuse_payload(req_id, name, reason="This hotline is read-only / scan-only."):
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": -32001,
            "message": "Tool '%s' is not allowed on this hotline. %s" % (name, reason),
            "data": {"tool": name, "allowed": sorted(ALLOWED_TOOLS)},
        },
    }
