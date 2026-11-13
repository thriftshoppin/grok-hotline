#!/usr/bin/env python3
"""Grok Hotline relay — public MCP front door on this Linux box.

The cyberdeck dials OUT (long-poll). Grok Bot connects to /mcp over
Streamable HTTP with a bearer token. This process never prints the token.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Any, Optional

from aiohttp import web

from allowlist import (
    ALLOWED_TOOLS,
    filter_tools_list_result,
    is_allowed_call,
    refuse_payload,
)

DIR = Path(__file__).resolve().parent
TOKEN_PATH = DIR / "mcp.token"
PAIR_PATH = DIR / "pair.code"
HOTLINE_PATH = DIR / "HOTLINE.txt"
LOG_DIR = DIR / "logs"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18765
PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
DECK_STALE_S = 120
HEARTBEAT_S = 30
FORWARD_TIMEOUT_S = 90
WAIT_FOR_DECK_S = 25
POLL_MAX_S = 25
PROTOCOL = "2024-11-05"

log = logging.getLogger("hotline")


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = logging.FileHandler(LOG_DIR / "relay.log", encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


def load_or_create_token() -> str:
    if TOKEN_PATH.exists():
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    os.chmod(TOKEN_PATH, 0o600)
    return token


def new_pair_code() -> str:
    code = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(6))
    fd = os.open(PAIR_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(code + "\n")
    os.chmod(PAIR_PATH, 0o600)
    return code


def bearer(request: web.Request) -> str:
    h = request.headers.get("Authorization") or ""
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return ""


def json_error(status: int, message: str, extra: Optional[dict] = None) -> web.Response:
    body: dict[str, Any] = {"error": message}
    if extra:
        body.update(extra)
    return web.json_response(body, status=status)


class Hotline:
    def __init__(self, token: str, pair_code: str) -> None:
        self.token = token
        self.pair_code = pair_code
        self.public_base: Optional[str] = None
        self._lock = asyncio.Lock()
        self.deck_token: Optional[str] = None
        self.deck_connected = False
        self.deck_full = False
        self.deck_last_seen = 0.0
        self.deck_queue: asyncio.Queue[dict] = asyncio.Queue()
        self.pending: dict[Any, asyncio.Future] = {}
        self.mcp_sessions: dict[str, float] = {}
        self.bad_pair_until = 0.0
        self.bad_pair_count = 0

    def deck_is_live(self) -> bool:
        if not self.deck_connected or not self.deck_token:
            return False
        if (time.time() - self.deck_last_seen) > DECK_STALE_S:
            return False
        return True

    def _drop_deck(self, reason: str) -> None:
        if self.deck_connected:
            log.info("Deck session dropped: %s", reason)
        self.deck_connected = False
        self.deck_token = None
        while not self.deck_queue.empty():
            try:
                self.deck_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        for fut in list(self.pending.values()):
            if not fut.done():
                fut.set_exception(TimeoutError(f"deck hung up: {reason}"))
        self.pending.clear()

    def touch_deck(self) -> None:
        self.deck_last_seen = time.time()

    def check_mcp_auth(self, request: web.Request) -> Optional[web.Response]:
        got = bearer(request)
        if not got or not hmac.compare_digest(got, self.token):
            log.warning("MCP auth rejected from %s", request.remote)
            return json_error(401, "unauthorized")
        return None

    def check_deck_auth(self, request: web.Request, body: Optional[dict] = None) -> Optional[web.Response]:
        if not self.deck_is_live():
            return json_error(409, "no live deck session")
        got = bearer(request)
        if body and not got:
            got = str((body or {}).get("deck_token") or "")
        if not got or not self.deck_token or not hmac.compare_digest(got, self.deck_token):
            return json_error(401, "unauthorized deck session")
        self.touch_deck()
        return None

    async def wait_for_deck(self, seconds: float) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.deck_is_live():
                return True
            await asyncio.sleep(0.2)
        return self.deck_is_live()

    async def enqueue_for_deck(self, message: dict) -> None:
        await self.deck_queue.put(message)

    async def forward_request(self, message: dict) -> dict:
        if not self.deck_is_live():
            return {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {"code": -32010, "message": "Deck is not connected to the hotline."},
            }
        req_id = message.get("id")
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending[req_id] = fut
        await self.enqueue_for_deck(message)
        try:
            return await asyncio.wait_for(fut, timeout=FORWARD_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.warning("Timed out waiting for deck reply id=%s method=%s", req_id, message.get("method"))
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32011, "message": "Timed out waiting for the deck."},
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32012, "message": f"Deck error: {type(e).__name__}"},
            }
        finally:
            self.pending.pop(req_id, None)

    def accept_reply(self, message: dict) -> None:
        if not isinstance(message, dict) or "id" not in message:
            log.info("Ignoring deck message with no id: keys=%s", list(message) if isinstance(message, dict) else type(message))
            return
        fut = self.pending.get(message.get("id"))
        if fut and not fut.done():
            fut.set_result(message)
        else:
            log.info("Deck reply id=%s had no waiter (late or duplicate)", message.get("id"))


hotline: Hotline


async def handle_health(_request: web.Request) -> web.Response:
    return web.json_response({
        "ok": True,
        "service": "grok-hotline",
        "deck_connected": hotline.deck_is_live(),
        "deck_mode": ("full" if hotline.deck_full else "read-only") if hotline.deck_is_live() else None,
        "public": bool(hotline.public_base),
    })


async def handle_mcp_post(request: web.Request) -> web.Response:
    denied = hotline.check_mcp_auth(request)
    if denied:
        return denied
    try:
        payload = await request.json()
    except Exception:
        return json_error(400, "invalid json")

    messages = payload if isinstance(payload, list) else [payload]
    if not messages or not all(isinstance(m, dict) for m in messages):
        return json_error(400, "invalid json-rpc")

    if not hotline.deck_is_live():
        log.info("MCP client waiting for a deck to call in (up to %ss)", WAIT_FOR_DECK_S)
        await hotline.wait_for_deck(WAIT_FOR_DECK_S)
        if not hotline.deck_is_live():
            return json_error(503, "deck not connected", {"hint": "Type the hotline address into grok_hotline.py on the deck first."})

    session = request.headers.get("Mcp-Session-Id")
    is_init = any(m.get("method") == "initialize" for m in messages)
    if is_init:
        session = secrets.token_hex(16)
        hotline.mcp_sessions[session] = time.time()
        log.info("MCP session started")
    elif session and session not in hotline.mcp_sessions:
        # Accept unknown session ids after a live initialize race, but
        # mint one if the client forgot.
        session = session or secrets.token_hex(16)
        hotline.mcp_sessions[session] = time.time()
    elif not session:
        session = secrets.token_hex(16)
        hotline.mcp_sessions[session] = time.time()

    replies: list[dict] = []
    saw_request = False
    for msg in messages:
        method = msg.get("method")
        req_id = msg.get("id")
        is_request = "id" in msg and method
        log.info("MCP → deck  method=%s id=%s", method or "(reply)", req_id)

        if method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name") if isinstance(params, dict) else None
            args = params.get("arguments") if isinstance(params, dict) else None
            if not hotline.deck_full and not is_allowed_call(name, args):
                log.info("Blocked tools/call name=%s args_action=%s", name, (args or {}).get("action") if isinstance(args, dict) else None)
                replies.append(refuse_payload(req_id, name))
                saw_request = True
                continue

        if is_request:
            saw_request = True
            reply = await hotline.forward_request(msg)
            if method == "tools/list" and not hotline.deck_full and isinstance(reply, dict) and "result" in reply:
                reply = dict(reply)
                reply["result"] = filter_tools_list_result(reply.get("result"))
                log.info(
                    "tools/list filtered to %s tools",
                    len((reply["result"] or {}).get("tools") or []) if isinstance(reply.get("result"), dict) else "?",
                )
            replies.append(reply)
        else:
            # Notification or client response: fire-and-forget to the deck.
            await hotline.enqueue_for_deck(msg)

    headers = {
        "Mcp-Session-Id": session,
        "MCP-Protocol-Version": request.headers.get("MCP-Protocol-Version", PROTOCOL),
        "Cache-Control": "no-store",
    }
    if not saw_request:
        return web.Response(status=202, headers=headers)
    body = replies[0] if not isinstance(payload, list) else replies
    return web.json_response(body, headers=headers)


async def handle_mcp_get(request: web.Request) -> web.Response:
    denied = hotline.check_mcp_auth(request)
    if denied:
        return denied
    session = request.headers.get("Mcp-Session-Id")
    if not session:
        return json_error(400, "missing Mcp-Session-Id")

    async def stream(resp: web.StreamResponse):
        # Keep the SSE stream open so Streamable HTTP clients that issue GET
        # after initialize do not fail. Server-initiated messages are unused
        # for this request/response relay.
        try:
            while True:
                await resp.write(b": keepalive\n\n")
                await asyncio.sleep(15)
        except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
            return

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Mcp-Session-Id": session,
        },
    )
    await resp.prepare(request)
    await stream(resp)
    return resp


async def handle_mcp_delete(request: web.Request) -> web.Response:
    denied = hotline.check_mcp_auth(request)
    if denied:
        return denied
    session = request.headers.get("Mcp-Session-Id")
    if session:
        hotline.mcp_sessions.pop(session, None)
        log.info("MCP session closed")
    return web.Response(status=204)


async def handle_deck_connect(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    code = str(body.get("code") or bearer(request) or "").strip().upper()
    now = time.time()
    if now < hotline.bad_pair_until:
        return json_error(429, "too many bad pairing attempts, wait a moment")
    if not code or not hmac.compare_digest(code, hotline.pair_code):
        hotline.bad_pair_count += 1
        if hotline.bad_pair_count >= 8:
            hotline.bad_pair_until = now + 15
            hotline.bad_pair_count = 0
        log.warning("Bad pairing code from %s", request.remote)
        return json_error(401, "bad pairing code")
    hotline.bad_pair_count = 0
    async with hotline._lock:
        if hotline.deck_is_live():
            # The caller proved the pairing code, so a re-dial replaces the old line.
            log.info("Deck re-dialed; replacing the old session")
        hotline._drop_deck("replaced")
        hotline.deck_token = secrets.token_urlsafe(24)
        hotline.deck_connected = True
        hotline.deck_full = bool(body.get("full"))
        hotline.touch_deck()
    log.info("Deck paired and on the line (%s)", "FULL control" if hotline.deck_full else "read-only")
    return web.json_response({
        "ok": True,
        "deck_token": hotline.deck_token,
        "poll": "/deck/poll",
        "reply": "/deck/reply",
        "hangup": "/deck/hangup",
        "poll_timeout_s": POLL_MAX_S,
    })


async def handle_deck_poll(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    denied = hotline.check_deck_auth(request, body)
    if denied:
        return denied
    # Optional piggybacked replies
    for msg in body.get("replies") or []:
        if isinstance(msg, dict):
            hotline.accept_reply(msg)
    timeout = body.get("timeout")
    try:
        timeout_s = float(timeout)
    except (TypeError, ValueError):
        timeout_s = POLL_MAX_S
    timeout_s = max(1.0, min(timeout_s, POLL_MAX_S))
    messages: list[dict] = []
    try:
        first = await asyncio.wait_for(hotline.deck_queue.get(), timeout=timeout_s)
        messages.append(first)
        while not hotline.deck_queue.empty():
            try:
                messages.append(hotline.deck_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
    except asyncio.TimeoutError:
        pass
    # If this poll's session was replaced while it waited, hand the messages
    # back to the queue for the new deck instead of losing them.
    if hotline.deck_token is None or bearer(request) not in ("", hotline.deck_token) or (
        not bearer(request) and str(body.get("deck_token") or "") != hotline.deck_token
    ):
        for m in messages:
            hotline.deck_queue.put_nowait(m)
        return json_error(401, "unauthorized deck session")
    return web.json_response({"ok": True, "messages": messages})


async def handle_deck_reply(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return json_error(400, "invalid json")
    if not isinstance(body, dict):
        return json_error(400, "invalid json")
    denied = hotline.check_deck_auth(request, body)
    if denied:
        return denied
    msgs = body.get("messages") or body.get("replies") or []
    if isinstance(msgs, dict):
        msgs = [msgs]
    n = 0
    for msg in msgs:
        if isinstance(msg, dict):
            hotline.accept_reply(msg)
            n += 1
    return web.json_response({"ok": True, "accepted": n})


async def handle_deck_hangup(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = {}
    denied = hotline.check_deck_auth(request, body if isinstance(body, dict) else {})
    if denied:
        return denied
    hotline._drop_deck("hangup")
    return web.json_response({"ok": True})


async def heartbeat_loop() -> None:
    """Ping the deck every HEARTBEAT_S so the line never goes idle."""
    n = 0
    misses = 0
    while True:
        await asyncio.sleep(HEARTBEAT_S)
        if not hotline.deck_is_live():
            misses = 0
            continue
        n += 1
        hb_id = "hb-%d" % n
        fut = asyncio.get_running_loop().create_future()
        hotline.pending[hb_id] = fut
        await hotline.deck_queue.put({"jsonrpc": "2.0", "id": hb_id, "method": "ping"})
        try:
            await asyncio.wait_for(fut, timeout=HEARTBEAT_S)
            if misses:
                log.info("Heartbeat back after %d miss(es)", misses)
            misses = 0
        except Exception:
            misses += 1
            log.warning("Heartbeat %s missed (%d in a row)", hb_id, misses)
        finally:
            hotline.pending.pop(hb_id, None)


async def _start_heartbeat(app: web.Application) -> None:
    app["heartbeat"] = asyncio.create_task(heartbeat_loop())


def build_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(_start_heartbeat)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/mcp", handle_mcp_post)
    app.router.add_get("/mcp", handle_mcp_get)
    app.router.add_delete("/mcp", handle_mcp_delete)
    app.router.add_post("/deck/connect", handle_deck_connect)
    app.router.add_post("/deck/poll", handle_deck_poll)
    app.router.add_post("/deck/reply", handle_deck_reply)
    app.router.add_post("/deck/hangup", handle_deck_hangup)
    return app


def write_hotline_file(public_base: str, pair_code: str) -> str:
    host = public_base
    if host.startswith("https://"):
        host = host[len("https://"):]
    elif host.startswith("http://"):
        host = host[len("http://"):]
    host = host.rstrip("/")
    address = f"{host}/{pair_code}"
    text = (
        "Grok Hotline\n"
        "============\n"
        f"Public MCP URL (for the Grok Bot connector, not the deck):\n"
        f"  {public_base.rstrip('/')}/mcp\n"
        "\n"
        "Type this address into grok_hotline.py on the deck:\n"
        f"  {address}\n"
        "\n"
        "The MCP bearer token is not stored in this file.\n"
        f"It lives in {TOKEN_PATH} (mode 600).\n"
        "\n"
        "The trycloudflare address can change if the tunnel restarts.\n"
    )
    fd = os.open(HOTLINE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(HOTLINE_PATH, 0o600)
    return address


async def start_tunnel(port: int, pair_code: str) -> asyncio.subprocess.Process:
    bin_path = shutil.which("cloudflared") or str(Path.home() / ".local/bin/cloudflared")
    if not Path(bin_path).exists():
        raise SystemExit(f"cloudflared not found at {bin_path}")
    log.info("Starting cloudflared quick tunnel to http://127.0.0.1:%s (HTTP/2 transport)", port)
    env = os.environ.copy()
    # This box blocks outbound QUIC/UDP 7844 to the edge; HTTP/2 over 443 works.
    env["TUNNEL_TRANSPORT_PROTOCOL"] = "http2"
    proc = await asyncio.create_subprocess_exec(
        bin_path, "tunnel", "--no-autoupdate", "--protocol", "http2",
        "--url", f"http://127.0.0.1:{port}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    url_re = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
    assert proc.stdout is not None
    deadline = time.time() + 45
    collected = []
    while time.time() < deadline:
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=2)
        except asyncio.TimeoutError:
            if proc.returncode is not None:
                break
            continue
        if not line:
            break
        text = line.decode("utf-8", "replace").rstrip()
        collected.append(text)
        # Do not log every cloudflared line at info; keep it short.
        if "ERR" in text or "INF" in text or "http" in text.lower():
            # Redact nothing sensitive here; URLs are the pairing host.
            if "trycloudflare.com" in text or "Registered" in text or "ERR" in text:
                log.info("cloudflared: %s", text)
        m = url_re.search(text)
        if m:
            public = m.group(0)
            hotline.public_base = public
            address = write_hotline_file(public, pair_code)
            log.info("Hotline is public. Pairing address: %s", address)
            # Keep draining logs in the background so the pipe does not fill.
            asyncio.create_task(_drain_tunnel(proc))
            return proc
    raise SystemExit("cloudflared did not print a trycloudflare URL in time")


async def _drain_tunnel(proc: asyncio.subprocess.Process) -> None:
    assert proc.stdout is not None
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if "ERR" in text or "error" in text.lower():
                log.warning("cloudflared: %s", text)
    except Exception:
        pass
    log.warning("cloudflared exited with code %s", proc.returncode)


async def main_async(argv: Optional[list[str]] = None) -> int:
    global hotline
    parser = argparse.ArgumentParser(description="Grok Hotline relay")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--tunnel", action="store_true", help="also start a cloudflared quick tunnel")
    args = parser.parse_args(argv)

    setup_logging()
    token = load_or_create_token()
    # Keep the same pairing code across relay restarts so a dialed deck can redial.
    pair_code = ""
    if PAIR_PATH.exists() and not os.environ.get("HOTLINE_NEW_CODE"):
        pair_code = PAIR_PATH.read_text(encoding="utf-8").strip().upper()
    if not pair_code:
        pair_code = new_pair_code()
    hotline = Hotline(token, pair_code)
    log.info("Relay starting on %s:%s (token file %s, pairing code written to %s)",
             args.host, args.port, TOKEN_PATH, PAIR_PATH)
    log.info("Allowed tools: %s", ", ".join(sorted(ALLOWED_TOOLS)))

    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    await site.start()
    log.info("Listening on http://%s:%s", args.host, args.port)

    tunnel_proc = None
    if args.tunnel:
        tunnel_proc = await start_tunnel(args.port, pair_code)

    stop = asyncio.Event()

    def _stop(*_a):
        log.info("Relay shutting down")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _stop())

    await stop.wait()
    if tunnel_proc and tunnel_proc.returncode is None:
        tunnel_proc.terminate()
        try:
            await asyncio.wait_for(tunnel_proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            tunnel_proc.kill()
    await runner.cleanup()
    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
