# Box-side relay (optional)

This folder is for the always-on Linux box that *receives* the call, not
for the Windows cyberdeck. The deck only needs `../deck/` and
`../install.cmd`.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python relay.py
```

The relay listens on `127.0.0.1:18765` by default. It writes `mcp.token`
and `pair.code` next to `relay.py` (mode 600) and a `HOTLINE.txt` once a
public URL is known. Those files are gitignored — do not commit them.

`relay.py --tunnel` starts a vanilla cloudflared quick tunnel. On some
networks that fails because system DNS rewrites `*.argotunnel.com`. In
that case use `run_public_tunnel.sh`, which bind-mounts a DoH-resolved
hosts file inside a user namespace and runs cloudflared over HTTP/2.

The public trycloudflare hostname can change after a restart. The MCP
bearer token is never printed.

## Tests

With the relay running and a deck (or `grok_hotline.py dial … --builtin`)
on the line:

```bash
.venv/bin/python test_hotline.py
```

`test_hotline.py` reads `mcp.token` at runtime and never prints it.
