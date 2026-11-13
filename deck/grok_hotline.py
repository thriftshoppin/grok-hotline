#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Grok Hotline deck client — stdlib only, Python 3.8+.

Dials OUT to the relay. If Eden is installed it is used (read-only
allowlist). Otherwise a built-in scan tool set runs in-process.

    grok dial <address>
    grok hangup
    grok find-python
    grok help

The file name contains "grok" so Eden labels this client as Grok.
This script never modifies a bridge install or its venv.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

# Identical to allowlist.py on the box. Kept inline so this file is standalone.
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
# Eden is the gate: by default every Eden tool is passed through (click, type,
# launch...) and Eden's own client approval and kill switch decide.
# `grok dial --read-only` restores the scan-only allowlist.
FULL_ACCESS = {"on": True}

DEFAULT_EDEN_ROOT = os.path.join(os.path.expanduser("~"), "tools", "eden-desktop-bridge")
SESSION_DIR_NAME = "grok-hotline"
POLL_TIMEOUT_S = 25
EDEN_REPLY_TIMEOUT_S = 90
CONNECT_TIMEOUT_S = 20
READ_FILE_MAX = 512 * 1024
LIST_DIR_MAX = 400
OUTPUT_CAP = 100000


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print("%s %s" % (ts, msg), flush=True)


def is_allowed_tool_name(name: Any) -> bool:
    if FULL_ACCESS["on"]:
        return True
    return str(name or "") in ALLOWED_TOOLS


def is_allowed_call(name: Any, arguments: Any = None) -> bool:
    name = str(name or "")
    if FULL_ACCESS["on"] and name:
        return True
    if name not in ALLOWED_TOOLS:
        return False
    if name == "desktop_audio_session":
        action = ""
        if isinstance(arguments, dict):
            action = arguments.get("action", "")
        return str(action).lower() in AUDIO_SESSION_ACTIONS
    return True


def filter_tools_list_result(result: Any) -> Any:
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


def refuse_payload(req_id: Any, name: Any) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": -32001,
            "message": (
                "Tool '%s' is not allowed on this hotline. "
                "This hotline was dialed with --read-only."
            ) % name,
            "data": {"tool": name, "allowed": sorted(ALLOWED_TOOLS)},
        },
    }


def tool_result(req_id: Any, payload: Any) -> dict:
    if isinstance(payload, (dict, list)):
        text = json.dumps(payload, ensure_ascii=True, indent=2)
    else:
        text = str(payload)
    if len(text) > OUTPUT_CAP:
        text = text[:OUTPUT_CAP] + "\n...[truncated]..."
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"content": [{"type": "text", "text": text}]},
    }


def tool_error(req_id: Any, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32002, "message": message},
    }


def session_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".")
    else:
        base = Path(os.environ.get("TMPDIR") or "/tmp")
    d = base / SESSION_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def session_path() -> Path:
    return session_dir() / "session.json"


def python_cache_path() -> Path:
    home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or ".")
    return home / "tools" / "grok" / "python_path.txt"


def write_session(data: dict) -> None:
    path = session_path()
    raw = json.dumps(data).encode("utf-8")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def read_session() -> Optional[dict]:
    path = session_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def clear_session() -> None:
    path = session_path()
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def parse_address(text: str) -> tuple:
    raw = (text or "").strip().strip("\"'")
    if not raw:
        raise ValueError("No hotline address given.")
    raw = raw.replace("#", "/")
    if " " in raw and "/" not in raw.split("://", 1)[-1]:
        host, _, code = raw.partition(" ")
        raw = host.rstrip("/") + "/" + code.strip()
    if not raw.lower().startswith("http://") and not raw.lower().startswith("https://"):
        raw = "https://" + raw
    no_query = raw.split("?", 1)[0].rstrip("/")
    if "/" not in no_query.split("://", 1)[-1]:
        raise ValueError(
            "Address must look like host.trycloudflare.com/ABC123 "
            "(hostname and a 6-character pairing code)."
        )
    base, _, code = no_query.rpartition("/")
    code = code.strip().upper()
    if len(code) < 4 or len(code) > 8 or not code.isalnum():
        raise ValueError(
            "Pairing code '%s' does not look right. Expected 6 letters or digits." % code
        )
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    return base.rstrip("/"), code


def http_json(url: str, payload: dict, token: Optional[str] = None, timeout: float = 30):
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "grok-hotline-deck/1.0",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            body = r.read()
            if not body:
                return r.status, None
            try:
                return r.status, json.loads(body.decode("utf-8"))
            except ValueError:
                return r.status, body.decode("utf-8", "replace")[:500]
    except urllib.error.HTTPError as e:
        raw = e.read() if e.fp else b""
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            parsed = raw.decode("utf-8", "replace")[:500] if raw else None
        return e.code, parsed
    except urllib.error.URLError as e:
        raise ConnectionError("Could not reach %s: %s" % (url, e.reason)) from e


def help_text() -> str:
    return """Grok Hotline

  grok dial <address>              Call in and start a read-only scan session
  grok dial <address> --python P   Use this python.exe (not on PATH is fine)
  grok find-python                 Search this PC for a Python we can use
  grok hangup                      Hang up a call started by grok dial
  grok help                        Show this help

Address looks like:  some-words.trycloudflare.com/X7K2P9

The deck only dials out. It never opens a port.
If Eden is installed it is used, with only scan tools allowed.
If not, built-in tools are used: sysinfo, processes, disks, network,
list_dir, read_file, screenshot. Click/type/run/writes are blocked.

Ctrl+C hangs up. If Eden's feed window is open, Esc still stops the bridge.

Python is often next to the Claude desktop bridge and not on PATH.
grok.cmd will search for it and remember the path in:
  %USERPROFILE%\\tools\\grok\\python_path.txt
"""


def pause_and_exit(code: int, no_pause: bool) -> None:
    log("Exit code: %s" % code)
    if (not no_pause) and os.name == "nt" and sys.stdin is not None and sys.stdin.isatty():
        try:
            input("Press Enter to close...")
        except Exception:
            time.sleep(8)
    sys.exit(code)


def _creation_flags() -> int:
    if os.name == "nt":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    return 0


def run_cmd(args, timeout=30):
    try:
        r = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            text=True,
            errors="replace",
            creationflags=_creation_flags(),
        )
        return r.stdout or "", r.stderr or "", r.returncode
    except FileNotFoundError:
        return "", "not found: %s" % args[0], 127
    except subprocess.TimeoutExpired:
        return "", "timeout", 124


# --------------- Python discovery (also used by: grok find-python) ---------------

def _looks_like_windowsapps(path: Path) -> bool:
    return "windowsapps" in str(path).lower()


def _python_version(path: Path) -> Optional[str]:
    if not path.is_file() or _looks_like_windowsapps(path):
        return None
    try:
        r = subprocess.run(
            [str(path), "-c", "import sys; print(sys.version.split()[0])"],
            capture_output=True,
            timeout=8,
            text=True,
            errors="replace",
            creationflags=_creation_flags(),
        )
        ver = (r.stdout or "").strip()
        if r.returncode == 0 and ver:
            return ver
    except Exception:
        return None
    return None


def iter_python_candidates():
    """Yield (path, label) for likely python.exe locations. Does not modify them."""
    home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or ".")
    local = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
    here = Path(__file__).resolve().parent
    seen = set()

    def add(p, label):
        try:
            rp = Path(p).expanduser()
        except Exception:
            return
        key = str(rp).lower()
        if key in seen:
            return
        seen.add(key)
        yield_box.append((rp, label))

    yield_box = []  # filled then yielded below

    env_py = os.environ.get("GROK_PYTHON") or os.environ.get("EDEN_PYTHON")
    if env_py:
        add(env_py, "GROK_PYTHON / EDEN_PYTHON")
    cache = python_cache_path()
    if cache.exists():
        try:
            add(cache.read_text(encoding="utf-8").strip(), "cache %s" % cache)
        except Exception:
            pass
    add(here / "python.exe", "next to grok_hotline.py")
    add(here / "runtime" / "python.exe", "runtime folder")
    add(sys.executable, "current interpreter")

    tools = home / "tools"
    if tools.is_dir():
        for d in tools.iterdir():
            add(d / ".venv" / "Scripts" / "python.exe", str(d / ".venv"))
            add(d / "venv" / "Scripts" / "python.exe", str(d / "venv"))
            add(d / "Scripts" / "python.exe", str(d / "Scripts"))
            add(d / "python.exe", str(d))
            add(d / "bin" / "python", str(d / "bin"))
    add(home / "tools" / "eden-desktop-bridge" / ".venv" / "Scripts" / "python.exe", "default Eden venv")

    for pattern in ("*bridge*", "tools/*bridge*"):
        for d in home.glob(pattern):
            if not d.is_dir():
                continue
            add(d / "python.exe", str(d))
            add(d / ".venv" / "Scripts" / "python.exe", str(d / ".venv"))
            add(d / "venv" / "Scripts" / "python.exe", str(d / "venv"))
            try:
                children = list(d.iterdir())
            except OSError:
                children = []
            for e in children:
                if e.is_dir():
                    add(e / "python.exe", str(e))
                    add(e / ".venv" / "Scripts" / "python.exe", str(e / ".venv"))
                    add(e / "Scripts" / "python.exe", str(e / "Scripts"))

    pyroot = local / "Programs" / "Python"
    if pyroot.is_dir():
        for d in pyroot.glob("Python3*"):
            add(d / "python.exe", str(d))

    # Return first batch now; walk heavier trees in cmd. Here do a bounded walk
    # of Documents\Codex and tools for python.exe.
    roots = [
        tools,
        home / "Documents" / "Codex",
        local / "Programs",
    ]
    for root in roots:
        if not root.is_dir():
            continue
        n = 0
        try:
            for dirpath, dirnames, filenames in os.walk(str(root)):
                low = dirpath.lower()
                if any(s in low for s in ("\\temp\\", "/temp/", "node_modules", "windowsapps", "__pycache__")):
                    dirnames[:] = []
                    continue
                # prune deep noise
                dirnames[:] = [dn for dn in dirnames if dn.lower() not in {".git", "node_modules", "__pycache__", "temp"}]
                if "python.exe" in filenames or "python" in filenames:
                    exe = Path(dirpath) / ("python.exe" if "python.exe" in filenames else "python")
                    add(exe, "walk %s" % root)
                    n += 1
                if n >= 40:
                    break
        except OSError:
            pass

    for item in yield_box:
        yield item


def cmd_find_python(_args: argparse.Namespace) -> int:
    print("grok: searching this PC for python.exe (will not change any install)")
    print()
    found = 0
    for path, label in iter_python_candidates():
        if not path.exists():
            continue
        ver = _python_version(path)
        if not ver:
            print("  SKIP  %s  (%s, not runnable or < 3.8)" % (path, label))
            continue
        print("  FOUND %s" % path)
        print("        version %s  (%s)" % (ver, label))
        found += 1
    if not found:
        print("No usable python.exe found.")
        print("See: https://www.python.org/downloads/windows/  (embeddable zip is enough)")
        return 1
    print()
    print('To use a listed path:  grok dial ADDRESS --python "C:\\path\\to\\python.exe"')
    print("Or set GROK_PYTHON. grok.cmd caches the first usable path in:")
    print("  %s" % python_cache_path())
    return 0


# --------------- Built-in read-only scan tools ---------------

def tool_sysinfo(_args: Optional[dict]) -> Any:
    info = {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "hostname": platform.node(),
    }
    if os.name == "nt":
        out, err, rc = run_cmd(
            ["cmd", "/c", "ver"], timeout=8
        )
        info["cmd_ver"] = (out or err).strip()
        out, _, _ = run_cmd(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_OperatingSystem) | "
                "Select-Object Caption,Version,BuildNumber,LastBootUpTime,TotalVisibleMemorySize,FreePhysicalMemory | "
                "ConvertTo-Json",
            ],
            timeout=20,
        )
        if out.strip():
            try:
                info["os"] = json.loads(out)
            except ValueError:
                info["os_raw"] = out.strip()[:4000]
        if not info.get("os"):
            out, _, _ = run_cmd(
                ["wmic", "os", "get", "Caption,Version,BuildNumber,TotalVisibleMemorySize,FreePhysicalMemory,LastBootUpTime", "/format:list"],
                timeout=15,
            )
            if out.strip():
                info["wmic_os"] = out.strip()[:4000]
        try:
            import ctypes
            from ctypes import wintypes

            class MEM(ctypes.Structure):
                _fields_ = [
                    ("dwLength", wintypes.DWORD),
                    ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_uint64),
                    ("ullAvailPhys", ctypes.c_uint64),
                    ("ullTotalPageFile", ctypes.c_uint64),
                    ("ullAvailPageFile", ctypes.c_uint64),
                    ("ullTotalVirtual", ctypes.c_uint64),
                    ("ullAvailVirtual", ctypes.c_uint64),
                    ("ullAvailExtendedVirtual", ctypes.c_uint64),
                ]

            mem = MEM()
            mem.dwLength = ctypes.sizeof(MEM)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
                info["ram_bytes"] = {
                    "total": int(mem.ullTotalPhys),
                    "available": int(mem.ullAvailPhys),
                    "load_pct": int(mem.dwMemoryLoad),
                }
            ticks = ctypes.windll.kernel32.GetTickCount64()
            info["uptime_seconds"] = int(ticks) // 1000
        except Exception as e:
            info["ctypes_note"] = type(e).__name__
    else:
        try:
            info["uptime_seconds"] = float(Path("/proc/uptime").read_text().split()[0])
        except Exception:
            pass
        try:
            mem = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith(("MemTotal:", "MemAvailable:", "MemFree:")):
                    parts = line.split()
                    mem[parts[0].rstrip(":")] = parts[1] + " " + (parts[2] if len(parts) > 2 else "")
            info["meminfo"] = mem
        except Exception:
            pass
        uname = shutil.which("uname")
        if uname:
            out, _, _ = run_cmd([uname, "-a"], timeout=5)
            info["uname"] = out.strip()
    return info


def tool_processes(_args: Optional[dict]) -> Any:
    if os.name == "nt":
        out, err, rc = run_cmd(["tasklist", "/fo", "csv"], timeout=20)
        if rc != 0 and not out:
            return {"error": err or "tasklist failed", "exit": rc}
        return {"source": "tasklist", "csv": out[:OUTPUT_CAP]}
    out, err, rc = run_cmd(["ps", "ax", "-o", "pid,ppid,rss,pcpu,user,comm,args"], timeout=15)
    if rc == 0 and out:
        return {"source": "ps", "text": out[:OUTPUT_CAP]}
    rows = []
    try:
        for p in Path("/proc").iterdir():
            if not p.name.isdigit():
                continue
            try:
                cmd = (p / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
                rows.append({"pid": p.name, "cmd": cmd[:300]})
            except Exception:
                continue
            if len(rows) >= 400:
                break
    except Exception as e:
        return {"error": str(e)}
    return {"source": "/proc", "processes": rows}


def tool_disks(_args: Optional[dict]) -> Any:
    results = []
    if os.name == "nt":
        out, _, rc = run_cmd(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_LogicalDisk | "
                "Select-Object DeviceID,FileSystem,Size,FreeSpace,VolumeName | ConvertTo-Json",
            ],
            timeout=20,
        )
        if out.strip():
            try:
                data = json.loads(out)
                return {"source": "Win32_LogicalDisk", "disks": data}
            except ValueError:
                results.append({"raw": out.strip()[:8000]})
        out, _, _ = run_cmd(
            ["wmic", "logicaldisk", "get", "DeviceID,FileSystem,FreeSpace,Size,VolumeName", "/format:list"],
            timeout=15,
        )
        if out.strip():
            return {"source": "wmic", "text": out.strip()[:OUTPUT_CAP]}
    else:
        out, _, rc = run_cmd(["df", "-h"], timeout=10)
        if rc == 0:
            return {"source": "df", "text": out[:OUTPUT_CAP]}
    # shutil fallback
    seen = set()
    for d in (Path("/"), Path("C:\\") if os.name == "nt" else Path("/"), Path.home()):
        try:
            rp = str(d.resolve())
        except Exception:
            rp = str(d)
        if rp in seen or not d.exists():
            continue
        seen.add(rp)
        try:
            u = shutil.disk_usage(str(d))
            results.append({"path": rp, "total": u.total, "used": u.used, "free": u.free})
        except OSError:
            continue
    return {"source": "shutil.disk_usage", "disks": results}


def tool_network(_args: Optional[dict]) -> Any:
    blobs = {}
    if os.name == "nt":
        out, _, _ = run_cmd(["ipconfig", "/all"], timeout=15)
        blobs["ipconfig"] = out[:OUTPUT_CAP]
        out, _, _ = run_cmd(["netstat", "-ano"], timeout=20)
        blobs["netstat"] = out[:OUTPUT_CAP]
    else:
        if shutil.which("ip"):
            out, _, _ = run_cmd(["ip", "addr"], timeout=10)
            blobs["ip_addr"] = out[:OUTPUT_CAP]
        elif shutil.which("ifconfig"):
            out, _, _ = run_cmd(["ifconfig", "-a"], timeout=10)
            blobs["ifconfig"] = out[:OUTPUT_CAP]
        if shutil.which("ss"):
            out, _, _ = run_cmd(["ss", "-tulpn"], timeout=10)
            blobs["ss"] = out[:OUTPUT_CAP]
        elif shutil.which("netstat"):
            out, _, _ = run_cmd(["netstat", "-tulpn"], timeout=10)
            blobs["netstat"] = out[:OUTPUT_CAP]
    return {"read_only": True, "data": blobs}


def tool_list_dir(args: Optional[dict]) -> Any:
    args = args or {}
    raw = args.get("path") or os.getcwd()
    path = Path(str(raw)).expanduser()
    try:
        path = path.resolve()
    except OSError as e:
        return {"error": str(e)}
    if not path.exists():
        return {"error": "not found: %s" % path}
    if not path.is_dir():
        return {"error": "not a directory: %s" % path}
    entries = []
    truncated = False
    try:
        for i, item in enumerate(path.iterdir()):
            if i >= LIST_DIR_MAX:
                truncated = True
                break
            rec = {"name": item.name, "is_dir": item.is_dir(), "is_file": item.is_file()}
            try:
                st = item.stat()
                rec["size"] = st.st_size
                rec["mtime"] = int(st.st_mtime)
            except OSError:
                rec["stat_error"] = True
            entries.append(rec)
    except OSError as e:
        return {"error": str(e)}
    return {"path": str(path), "count": len(entries), "truncated": truncated, "entries": entries}


def tool_read_file(args: Optional[dict]) -> Any:
    args = args or {}
    raw = args.get("path")
    if not raw:
        return {"error": "path is required"}
    path = Path(str(raw)).expanduser()
    try:
        path = path.resolve()
    except OSError as e:
        return {"error": str(e)}
    if not path.exists() or not path.is_file():
        return {"error": "not a file: %s" % path}
    try:
        size = path.stat().st_size
    except OSError as e:
        return {"error": str(e)}
    if size > READ_FILE_MAX:
        return {"error": "file is %s bytes; limit is %s (read-only cap)" % (size, READ_FILE_MAX)}
    # Skip obvious device/special files
    if size > 0 and not path.is_file():
        return {"error": "refusing special file"}
    try:
        data = path.read_bytes()
    except OSError as e:
        return {"error": str(e)}
    encoding = str(args.get("encoding") or "utf-8")
    if encoding.lower() == "base64":
        return {"path": str(path), "size": size, "encoding": "base64", "content": base64.b64encode(data).decode("ascii")}
    try:
        text = data.decode(encoding)
        return {"path": str(path), "size": size, "encoding": encoding, "content": text}
    except UnicodeDecodeError:
        return {
            "path": str(path),
            "size": size,
            "encoding": "base64",
            "note": "could not decode as %s; returning base64" % encoding,
            "content": base64.b64encode(data).decode("ascii"),
        }


def tool_screenshot(_args: Optional[dict]) -> Any:
    if os.name == "nt":
        ps = r"""
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$s = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$b = New-Object System.Drawing.Bitmap $s.Width, $s.Height
$g = [System.Drawing.Graphics]::FromImage($b)
$g.CopyFromScreen($s.Location, [System.Drawing.Point]::Empty, $b.Size)
$p = Join-Path $env:TEMP ('grok-hotline-shot-{0}.png' -f [guid]::NewGuid().ToString('N'))
$b.Save($p, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $b.Dispose()
Write-Output $p
"""
        out, err, rc = run_cmd(
            ["powershell", "-NoProfile", "-Command", ps],
            timeout=30,
        )
        path = out.strip().splitlines()[-1].strip() if out.strip() else ""
        if rc != 0 or not path or not Path(path).is_file():
            return {"error": "screenshot failed", "stderr": err[:2000], "stdout": out[:500]}
        data = Path(path).read_bytes()
        try:
            Path(path).unlink()
        except OSError:
            pass
        b64 = base64.b64encode(data).decode("ascii")
        return {"format": "png", "bytes": len(data), "base64": b64}
    # Linux best-effort: no extra deps. Try ImageMagick or grim if present.
    tmp = Path(tempfile.gettempdir()) / ("grok-hotline-shot-%s.png" % os.getpid())
    for cmd in (
        ["import", "-window", "root", str(tmp)],
        ["grim", str(tmp)],
        ["scrot", str(tmp)],
    ):
        if not shutil.which(cmd[0]):
            continue
        out, err, rc = run_cmd(cmd, timeout=20)
        if rc == 0 and tmp.is_file():
            data = tmp.read_bytes()
            try:
                tmp.unlink()
            except OSError:
                pass
            return {"format": "png", "bytes": len(data), "base64": base64.b64encode(data).decode("ascii"), "via": cmd[0]}
    return {"error": "screenshot is not available on this OS without a capture tool"}


BUILTIN_DISPATCH = {
    "sysinfo": tool_sysinfo,
    "processes": tool_processes,
    "disks": tool_disks,
    "network": tool_network,
    "list_dir": tool_list_dir,
    "read_file": tool_read_file,
    "screenshot": tool_screenshot,
}


def builtin_tool_defs():
    schemas = {
        "sysinfo": ("OS, build, CPU, RAM, uptime", {}),
        "processes": ("Running processes (read-only)", {}),
        "disks": ("Logical disks and free space", {}),
        "network": ("ipconfig/ip addr and netstat/ss, read-only", {}),
        "list_dir": ("List a directory (read-only, capped)", {"path": {"type": "string"}}),
        "read_file": ("Read a file (read-only, size-capped)", {"path": {"type": "string", "description": "File to read"}, "encoding": {"type": "string"}}),
        "screenshot": ("Primary-display screenshot as PNG base64", {}),
    }
    tools = []
    for name in sorted(BUILTIN_TOOLS):
        desc, props = schemas[name]
        required = ["path"] if name in {"read_file"} else []
        tools.append({
            "name": name,
            "description": desc,
            "inputSchema": {"type": "object", "properties": props, "required": required} if required else {"type": "object", "properties": props},
        })
    return tools


class BuiltinServer:
    """In-process MCP server for when Eden is not on this PC."""

    def handle(self, msg: dict) -> Optional[dict]:
        method = msg.get("method")
        req_id = msg.get("id")
        if method == "initialize":
            ver = ((msg.get("params") or {}).get("protocolVersion") or "2024-11-05")
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": ver,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "grok-hotline-builtin", "version": "1.0"},
                },
            }
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": builtin_tool_defs()}}
        if method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") if isinstance(params, dict) else {}
            if not is_allowed_call(name, arguments):
                return refuse_payload(req_id, name)
            fn = BUILTIN_DISPATCH.get(str(name))
            if not fn:
                return refuse_payload(req_id, name)
            try:
                payload = fn(arguments if isinstance(arguments, dict) else {})
            except Exception:
                log("Built-in tool %s failed:\n%s" % (name, traceback.format_exc()))
                return tool_error(req_id, "Tool '%s' failed: %s" % (name, sys.exc_info()[0].__name__))
            if isinstance(payload, dict) and payload.get("error") and len(payload) == 1:
                return tool_error(req_id, str(payload["error"]))
            return tool_result(req_id, payload)
        if "id" in msg:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Unknown method %s" % method}}
        return None


# --------------- Optional Eden stdio proxy ---------------

class EdenStdio:
    def __init__(self, python: Path, launcher: Path, cwd: Optional[Path]) -> None:
        self.python = python
        self.launcher = launcher
        self.cwd = cwd
        self.proc = None  # type: Optional[subprocess.Popen]
        self._q = []  # type: list
        self._cv = threading.Condition()
        self._alive = True
        self._thread = None  # type: Optional[threading.Thread]

    def start(self) -> None:
        if not self.python.exists():
            raise FileNotFoundError("Python not found: %s" % self.python)
        if not self.launcher.exists():
            raise FileNotFoundError("Launcher not found: %s" % self.launcher)
        log("Starting Eden launcher: %s %s" % (self.python, self.launcher))
        kwargs = dict(
            args=[str(self.python), str(self.launcher)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            cwd=str(self.cwd) if self.cwd and self.cwd.exists() else None,
            bufsize=0,
        )
        self.proc = subprocess.Popen(**kwargs)
        self._thread = threading.Thread(target=self._reader, name="eden-stdio", daemon=True)
        self._thread.start()
        log("Eden launcher pid=%s" % self.proc.pid)

    def _reader(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        stream = self.proc.stdout
        try:
            while self._alive:
                msg = _read_one_mcp(stream)
                if msg is None:
                    log("Eden stdout closed")
                    break
                with self._cv:
                    self._q.append(msg)
                    self._cv.notify_all()
        except Exception:
            log("Eden stdout reader error:\n%s" % traceback.format_exc())
        finally:
            self._alive = False
            with self._cv:
                self._cv.notify_all()

    def send(self, msg: dict) -> None:
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("Eden is not running")
        line = json.dumps(msg, ensure_ascii=True) + "\n"
        self.proc.stdin.write(line.encode("utf-8"))
        self.proc.stdin.flush()

    def wait_id(self, req_id: Any, timeout: float) -> Optional[dict]:
        deadline = time.time() + timeout
        with self._cv:
            while True:
                for i, msg in enumerate(self._q):
                    if msg.get("id") == req_id:
                        return self._q.pop(i)
                remaining = deadline - time.time()
                if remaining <= 0 or not self._alive:
                    return None
                self._cv.wait(remaining)

    def stop(self) -> None:
        self._alive = False
        if not self.proc:
            return
        if self.proc.poll() is None:
            log("Stopping Eden launcher pid=%s (the guardian stays up)" % self.proc.pid)
            try:
                self.proc.terminate()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except OSError:
                    pass


def _read_one_mcp(stream) -> Optional[dict]:
    first = stream.read(1)
    if not first:
        return None
    buf = first
    if first in (b"C", b"c"):
        while not (buf.endswith(b"\r\n\r\n") or buf.endswith(b"\n\n")):
            ch = stream.read(1)
            if not ch:
                return None
            buf += ch
            if len(buf) > 4096:
                break
        if b"content-length:" in buf.lower():
            length = None
            for line in buf.replace(b"\r\n", b"\n").decode("utf-8", "replace").split("\n"):
                if line.lower().startswith("content-length:"):
                    try:
                        length = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        length = None
            if length is not None and 0 < length < 8_000_000:
                body = b""
                while len(body) < length:
                    chunk = stream.read(length - len(body))
                    if not chunk:
                        break
                    body += chunk
                return json.loads(body.decode("utf-8"))
    while not buf.endswith(b"\n"):
        ch = stream.read(1)
        if not ch:
            break
        buf += ch
        if len(buf) > 8_000_000:
            raise RuntimeError("Eden message too large")
    line = buf.decode("utf-8", "replace").strip()
    if not line:
        return _read_one_mcp(stream)
    return json.loads(line)


def find_eden(args: argparse.Namespace):
    """Return (root, python, launcher) or None. Never modifies the install."""
    if getattr(args, "builtin", False):
        return None
    root = Path(args.eden_root or os.environ.get("EDEN_ROOT") or DEFAULT_EDEN_ROOT)
    python_override = args.eden_python or os.environ.get("EDEN_PYTHON")
    launcher_override = args.launcher or os.environ.get("EDEN_LAUNCHER")
    if launcher_override:
        ln = Path(launcher_override)
    else:
        ln = root / "launcher.py"
        if not ln.exists():
            # Maybe EDEN_ROOT wasn't set but we can infer from a known venv python.
            return None
    if not ln.exists():
        return None
    if python_override:
        py = Path(python_override)
    else:
        if os.name == "nt":
            py = root / ".venv" / "Scripts" / "python.exe"
        else:
            py = root / ".venv" / "bin" / "python"
        if not py.exists():
            # Reuse the interpreter running this script if it lives in that tree.
            exe = Path(sys.executable)
            if "eden" in str(exe).lower() or str(root).lower() in str(exe).lower():
                py = exe
    if not py.exists():
        log("Eden launcher found at %s but no venv python next to it; using built-in tools." % ln)
        return None
    return (ln.parent if args.eden_root is None else root, py, ln)


def process_message(handler, msg: dict) -> Optional[dict]:
    if not isinstance(msg, dict):
        return None
    method = msg.get("method")
    req_id = msg.get("id")
    is_request = "id" in msg and method
    if method == "ping":
        # Heartbeat from the relay; answer here so Eden is not bothered.
        return {"jsonrpc": "2.0", "id": req_id, "result": {}} if "id" in msg else None
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name") if isinstance(params, dict) else None
        args = params.get("arguments") if isinstance(params, dict) else None
        if not is_allowed_call(name, args):
            log("Blocked tools/call name=%s" % name)
            return refuse_payload(req_id, name)
        log("Call %s id=%s" % (name, req_id))
    elif method:
        log("MCP %s id=%s" % (method, req_id))

    if isinstance(handler, BuiltinServer):
        return handler.handle(msg)

    # Eden stdio
    eden = handler
    eden.send(msg)
    if not is_request:
        return None
    reply = eden.wait_id(req_id, EDEN_REPLY_TIMEOUT_S)
    if reply is None:
        log("Eden did not reply to id=%s in time" % req_id)
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32011, "message": "Eden did not reply in time."},
        }
    if method == "tools/list" and isinstance(reply.get("result"), dict):
        reply = dict(reply)
        reply["result"] = filter_tools_list_result(reply.get("result"))
        n = len((reply["result"] or {}).get("tools") or [])
        log("tools/list filtered to %s allowed tools" % n)
    return reply


def cmd_help(_args: argparse.Namespace) -> int:
    print(help_text(), end="")
    return 0


def _kill_pid(pid: int, label: str) -> None:
    if pid <= 0:
        return
    log("hangup: stopping %s pid=%s" % (label, pid))
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        time.sleep(0.4)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def cmd_hangup(_args: argparse.Namespace) -> int:
    sess = read_session()
    if not sess:
        log("No live hotline session on this PC.")
        return 1
    token = sess.get("deck_token")
    base = sess.get("base")
    if base and token:
        try:
            status, body = http_json(base.rstrip("/") + "/deck/hangup", {}, token=token, timeout=8)
            log("Relay hangup HTTP %s" % status)
        except Exception as e:
            log("Relay hangup skipped: %s: %s" % (type(e).__name__, e))
    launcher_pid = sess.get("launcher_pid")
    pid = sess.get("pid")
    if launcher_pid and launcher_pid != os.getpid():
        _kill_pid(int(launcher_pid), "Eden launcher")
    if pid and pid != os.getpid():
        _kill_pid(int(pid), "grok hotline")
    clear_session()
    log("Hung up.")
    return 0


def cmd_dial(args: argparse.Namespace) -> int:
    address = args.address
    if not address:
        try:
            address = input("Enter hotline address: ").strip()
        except EOFError:
            log("No address given.")
            return 2
    try:
        base, code = parse_address(address)
    except ValueError as e:
        log(str(e))
        return 2

    # Local HTTP is allowed for tests (http://127.0.0.1:port/CODE) — parse_address
    # already preserved the scheme.
    if base.startswith("http://") and "127.0.0.1" not in base and "localhost" not in base:
        log("Refusing cleartext http to a non-local host.")
        return 2

    log("Dialing %s ..." % base)
    FULL_ACCESS["on"] = not bool(getattr(args, "read_only", False))
    if FULL_ACCESS["on"]:
        log("Full control: Eden is the gate. Esc twice in Eden (or Ctrl+Alt+Pause) stops Grok; Ctrl+C here hangs up.")
    else:
        log("Read-only mode: scan tools only.")
    status, body = http_json(base + "/deck/connect", {"code": code, "full": FULL_ACCESS["on"]}, timeout=CONNECT_TIMEOUT_S)
    if status != 200 or not isinstance(body, dict) or not body.get("ok"):
        log("Pairing failed HTTP %s: %s" % (status, body if body is not None else "(no body)"))
        return 3
    deck_token = body.get("deck_token")
    if not deck_token:
        log("Relay did not return a session token.")
        return 3
    log("Paired.")

    eden_info = find_eden(args)
    handler = None  # type: Any
    launcher_pid = None
    if eden_info:
        root, python, launcher = eden_info
        log("Eden found at %s — proxying with %s." % (launcher, "FULL control" if FULL_ACCESS["on"] else "the read-only allowlist"))
        handler = EdenStdio(python, launcher, cwd=root)
        try:
            handler.start()
            launcher_pid = handler.proc.pid if handler.proc else None
        except Exception:
            log("Failed to start Eden, falling back to built-in tools:\n%s" % traceback.format_exc())
            handler = BuiltinServer()
    else:
        log("Eden not found on this PC. Using built-in read-only scan tools.")
        handler = BuiltinServer()

    write_session({
        "pid": os.getpid(),
        "launcher_pid": launcher_pid,
        "base": base,
        "deck_token": deck_token,
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "eden" if isinstance(handler, EdenStdio) else "builtin",
    })
    log("On the line. Waiting for Grok. Ctrl+C hangs up.")

    hanging_up = {"flag": False}

    def hangup_cleanup() -> None:
        if hanging_up["flag"]:
            return
        hanging_up["flag"] = True
        log("Hanging up...")
        try:
            http_json(base + "/deck/hangup", {}, token=deck_token, timeout=8)
        except Exception as e:
            log("Relay hangup skipped: %s" % type(e).__name__)
        if isinstance(handler, EdenStdio):
            handler.stop()
        clear_session()
        log("Hung up.")

    def handle_sig(_s, _f) -> None:
        hangup_cleanup()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, handle_sig)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_sig)

    net_fail = {"n": 0}
    try:
        while True:
            if isinstance(handler, EdenStdio) and handler.proc and handler.proc.poll() is not None:
                log("Eden launcher exited with code %s" % handler.proc.returncode)
                return 5
            try:
                status, body = http_json(
                    base + "/deck/poll",
                    {"timeout": POLL_TIMEOUT_S},
                    token=deck_token,
                    timeout=POLL_TIMEOUT_S + 10,
                )
            except Exception as e:  # network blips, TLS resets, read timeouts
                backoff = min(15, 2 + net_fail["n"] * 2)
                net_fail["n"] += 1
                log("Line crackled (%s: %s). Retrying in %ss..." % (type(e).__name__, e, backoff))
                time.sleep(backoff)
                continue
            if status == 401:
                log("Another dial took over this hotline, so this window is hanging up.")
                return 6
            if status == 409:
                log("Relay dropped the session (it may have restarted). Redialing...")
                new_token = None
                for attempt in range(1, 31):
                    try:
                        cst, cbody = http_json(base + "/deck/connect", {"code": code, "full": FULL_ACCESS["on"]}, timeout=CONNECT_TIMEOUT_S)
                    except Exception as e:
                        cst, cbody = None, "%s: %s" % (type(e).__name__, e)
                    if cst == 200 and isinstance(cbody, dict) and cbody.get("deck_token"):
                        new_token = cbody["deck_token"]
                        break
                    if cst == 401:
                        log("Pairing code no longer accepted. Ask Grok for a new address.")
                        return 6
                    log("Redial attempt %s failed (%s). Retrying in 5s..." % (attempt, cbody))
                    time.sleep(5)
                if not new_token:
                    log("Could not get back on the line.")
                    return 6
                deck_token = new_token
                write_session({
                    "pid": os.getpid(),
                    "launcher_pid": launcher_pid,
                    "base": base,
                    "deck_token": deck_token,
                    "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "mode": "eden" if isinstance(handler, EdenStdio) else "builtin",
                })
                log("Back on the line.")
                continue
            if status != 200 or not isinstance(body, dict):
                log("Poll HTTP %s: %s. Retrying in 2s..." % (status, body))
                time.sleep(2)
                continue
            if net_fail["n"]:
                log("Line is clear again.")
                net_fail["n"] = 0
            messages = body.get("messages") or []
            replies = []
            for msg in messages:
                reply = process_message(handler, msg)
                if reply is not None:
                    replies.append(reply)
            if replies:
                try:
                    rstatus, rbody = http_json(
                        base + "/deck/reply",
                        {"messages": replies},
                        token=deck_token,
                        timeout=20,
                    )
                    if rstatus != 200:
                        log("Reply POST HTTP %s: %s" % (rstatus, rbody))
                except Exception as e:
                    log("Reply POST failed: %s: %s" % (type(e).__name__, e))
    except SystemExit:
        raise
    except Exception:
        log("Hotline error:\n%s" % traceback.format_exc())
        return 7
    finally:
        hangup_cleanup()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="grok",
        description="Grok Hotline: dial out from the deck to Grok Bot.",
        add_help=False,
    )
    p.add_argument("command", nargs="?", default=None, help="dial | hangup | help | find-python")
    p.add_argument("address", nargs="?", default=None, help="hotline address (host/CODE)")
    p.add_argument("--address", dest="address_flag", default=None, help=argparse.SUPPRESS)
    p.add_argument("--eden-root", default=None)
    p.add_argument("--python", dest="python_flag", default=None,
                   help="python.exe to remember / report; grok.cmd uses this to launch this script")
    p.add_argument("--eden-python", default=None, help="Python used to spawn the Eden launcher, if present")
    p.add_argument("--launcher", default=None, help="Path to launcher.py (optional)")
    p.add_argument("--builtin", action="store_true", help="Force built-in scan tools even if Eden exists")
    p.add_argument("--read-only", dest="read_only", action="store_true", help="Scan-only: block click, type, keys and launch")
    p.add_argument("--full", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--no-pause", action="store_true", help="Do not wait for Enter on exit")
    p.add_argument("-h", "--help", action="store_true")
    return p


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.address_flag and not args.address:
        args.address = args.address_flag
    # --python is consumed by grok.cmd; if passed through, also treat as eden python fallback
    if args.python_flag and not args.eden_python:
        args.eden_python = args.python_flag
    cmd = (args.command or "").lower()
    if cmd and cmd not in {"dial", "hangup", "help", "find-python", "find_python", "-h", "--help"} and not args.address:
        if "/" in cmd or "." in cmd:
            args.address = args.command
            cmd = "dial"
    no_pause = bool(args.no_pause)
    try:
        if args.help or cmd in {"help", "-h", "--help"}:
            return cmd_help(args)
        if cmd in {"find-python", "find_python"}:
            return cmd_find_python(args)
        if cmd in {"hangup", "hang-up"}:
            return cmd_hangup(args)
        if cmd in {"", "dial"}:
            return cmd_dial(args)
        log("Unknown command '%s'. Try: grok help" % args.command)
        return 2
    except KeyboardInterrupt:
        log("Interrupted.")
        return 0
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 0
    except Exception:
        log("Unhandled error:\n%s" % traceback.format_exc())
        if not no_pause:
            pause_and_exit(1, no_pause)
        return 1


if __name__ == "__main__":
    _no_pause = "--no-pause" in sys.argv
    try:
        code = main()
    except Exception:
        log("Fatal error:\n%s" % traceback.format_exc())
        code = 1
    if code not in (0,) and not _no_pause:
        pause_and_exit(code, _no_pause)
    elif code != 0:
        log("Exit code: %s" % code)
    sys.exit(code)
