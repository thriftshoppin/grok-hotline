#!/usr/bin/env python3
"""Write /tmp/hotline-cf-hosts with DoH-resolved Cloudflare edge names."""
import json
import urllib.request
from pathlib import Path

NAMES = [
    "region1.v2.argotunnel.com",
    "region2.v2.argotunnel.com",
    "cfd-features.argotunnel.com",
    "api.trycloudflare.com",
    "api.cloudflare.com",
]

def doh_a(name):
    url = "https://1.1.1.1/dns-query?name=%s&type=A" % name
    req = urllib.request.Request(url, headers={"accept": "application/dns-json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read().decode())
    return [a.get("data") for a in data.get("Answer") or [] if a.get("type") == 1]

lines = Path("/etc/hosts").read_text(encoding="utf-8", errors="replace").splitlines()
for n in NAMES:
    try:
        ips = doh_a(n)
    except Exception:
        ips = []
    if ips:
        # All A records so getent/cloudflared can use any
        for ip in ips:
            lines.append("%s %s" % (ip, n))
out = Path("/tmp/hotline-cf-hosts")
out.write_text("\n".join(lines) + "\n")
print("wrote", out, "names", NAMES)
