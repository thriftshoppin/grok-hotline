#!/usr/bin/env bash
# Persistent cloudflared quick tunnel with DoH hosts override.
# This box rewrites *.argotunnel.com to 198.18.0.1 in system DNS; we bind-mount
# a hosts file resolved via 1.1.1.1 inside a user namespace.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$DIR/logs/public-tunnel.log"
export PATH="/home/box/.local/bin:$PATH"
mkdir -p "$DIR/logs"
echo "$(date -Iseconds) public-tunnel supervisor starting" >>"$LOG"

update_hotline() {
  local url="$1"
  local py="$DIR/.venv/bin/python"
  "$py" - "$url" "$DIR" <<'PY'
import os, sys
from pathlib import Path
public, d = sys.argv[1], Path(sys.argv[2])
code = (d / "pair.code").read_text().strip()
# reuse relay helper if possible
sys.path.insert(0, str(d))
try:
    import relay
    addr = relay.write_hotline_file(public, code)
except Exception:
    host = public.replace("https://", "").replace("http://", "").rstrip("/")
    addr = f"{host}/{code}"
    text = (
        "Grok Hotline\n============\n"
        f"Public MCP URL (for the Grok Bot connector, not the deck):\n"
        f"  {public.rstrip('/')}/mcp\n\n"
        "Type this address into grok dial on the deck:\n"
        f"  {addr}\n\n"
        "The MCP bearer token is not stored in this file.\n"
        f"It lives in {d / 'mcp.token'} (mode 600).\n\n"
        "The public address can change if the tunnel restarts.\n"
    )
    fd = os.open(str(d / "HOTLINE.txt"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(d / "HOTLINE.txt", 0o600)
print(addr)
PY
}

while true; do
  "$DIR/.venv/bin/python" "$DIR/update_cf_hosts.py" >>"$LOG" 2>&1 || true
  if [ ! -s /tmp/hotline-cf-hosts ]; then
    echo "$(date -Iseconds) hosts file missing, retry in 5s" >>"$LOG"
    sleep 5
    continue
  fi
  echo "$(date -Iseconds) starting cloudflared (http2, ipv4, DoH hosts)" >>"$LOG"
  # Parse URL while the process runs
  unshare --user --map-root-user --mount bash -c '
    mount --bind /tmp/hotline-cf-hosts /etc/hosts
    export TUNNEL_TRANSPORT_PROTOCOL=http2
    exec /home/box/.local/bin/cloudflared tunnel --no-autoupdate --protocol http2 --edge-ip-version 4 --url http://127.0.0.1:18765
  ' 2>&1 | while IFS= read -r line; do
    echo "$line" >>"$LOG"
    case "$line" in
      *https://*.trycloudflare.com*)
        url=$(printf '%s\n' "$line" | grep -oE 'https://[a-z0-9-]+\.trycloudflare.com' | head -1)
        if [ -n "$url" ]; then
          echo "$(date -Iseconds) captured URL $url" >>"$LOG"
          update_hotline "$url" >>"$LOG" 2>&1 || true
        fi
        ;;
    esac
  done
  ec=${PIPESTATUS[0]}
  echo "$(date -Iseconds) cloudflared exited $ec; restarting in 3s" >>"$LOG"
  sleep 3
done
