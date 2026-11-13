# Grok Hotline

A small Windows client that dials Grok Bot and hands it your Eden desktop bridge for the session.

The public HTTPS endpoint runs on Grok Bot's computer, not on the deck.
The deck only dials out; it never opens a port. The client is two files
(`grok.cmd` + `grok_hotline.py`) and uses the Python standard library
only (Python 3.8+, including 3.14).

If the Eden desktop bridge is present, the client starts it and proxies
every Eden tool, with Eden's approval and kill switch as the gate. If Eden is not installed, built-in
read-only tools are used instead.

Python does not need to be on PATH. `grok.cmd` will search the Python
that already runs the Claude desktop bridge (tools venvs, *bridge*
folders, Documents\\Codex, official CPython installs), and cache the
path in `%USERPROFILE%\\tools\\grok\\python_path.txt`. It only *runs*
that `python.exe`. It does not install packages or modify the bridge or
its venv.

## Install

If you are Claude Code (or another agent) installing this on the deck, read `AGENTS.md` and follow it.

```bat
git clone https://github.com/thriftshoppin/grok-hotline.git
cd grok-hotline
install.cmd
```

`install.cmd` creates `%USERPROFILE%\\tools\\grok`, copies the deck
files there, and adds that folder to the *user* PATH. It prefers
PowerShell's `[Environment]::SetEnvironmentVariable` so the path is not
truncated. If PowerShell is missing, it falls back to `setx` only after
a length check. On error it prints the exit code and pauses.

Open a **new** terminal after install so it picks up the updated PATH.

## Use

```bat
grok find-python
grok dial <address>
```

Grok Bot will give you the `<address>` for the session (host plus a
short pairing code). Other commands:

```bat
grok dial <address> --python "C:\path\to\python.exe"
grok hangup
grok help
```

`Ctrl+C` also hangs up. The public address can change if the tunnel on
Grok Bot's computer restarts; ask for a fresh address if a call will
not go through.

If no Python is found, run `grok find-python`, pass `--python`, or drop
an official [Windows embeddable package](https://www.python.org/downloads/windows/)
into `%USERPROFILE%\\tools\\grok` so `python.exe` sits next to `grok.cmd`.

## Safety / allowlist

By default Eden is the gate (see below). With `--read-only`, clicking, typing, running programs, and file
writes are refused on both the deck and the relay.

When Eden is used, only these tools are allowed:

- `desktop_session_status`, `desktop_monitors_list`, `desktop_screenshot`
- `desktop_zoom`, `desktop_windows_list`, `desktop_ui_tree`, `desktop_find`
- `desktop_audio_level`, `desktop_audio_analyze`
- `desktop_audio_session` (only the `status` and `check` actions)

When Eden is not installed, the built-in tools are:

- `sysinfo`, `processes`, `disks`, `network`
- `list_dir`, `read_file`, `screenshot`

Everything else (including `desktop_run`) is rejected.

## Relay (optional)

The `relay/` folder is for the Linux box that hosts the public endpoint.
You do not need it on the deck. See `relay/README.md`.

## Eden is the gate

By default the hotline passes every Eden tool through, including click, type,
keys, scroll, window focus and launch. Eden decides: its one-time client
approval and its kill switch (Esc twice, Ctrl+Alt+Pause, Ctrl+Alt+Shift+K)
apply to Grok like any other client, and Ctrl+C in the hotline window hangs up.
`grok dial <address> --read-only` limits Grok to looking (screenshots, windows,
UI tree, audio levels, files).
After `git pull`, run `install.cmd` again so the copy in `%USERPROFILE%\tools\grok` updates.
