# AGENTS.md

Instructions for Claude Code (or any coding agent) installing and verifying
Grok Hotline on this cyberdeck. Read this file before you do anything else.

## Context

This machine is a Tiny11 Pro cyberdeck: Windows under the hood, but
stripped down. There is no winget, and you must not treat it like a full
Windows install.

Python 3.14 *is* present, but it is **not on PATH**. It already runs the
existing Claude / Eden desktop bridge. Do not install another Python.

The user's main PC is offline and this deck has none of its files.
Everything you need is in this repo. Do not wait for, or copy from,
another computer.

Grok Bot hosts the public endpoint. This deck only dials out with
`grok dial <address>`. You will not have a pairing address unless the
user gives you one in this conversation.

## Hard rules

1. Do **not** install packages. No `pip`, no `winget`, no `choco`, no
   modifying or creating venvs, no adding dependencies.
2. Do **not** modify the Eden / desktop bridge, its code, or its venv.
   You may *run* the bridge's `python.exe`. You may not change it.
3. Do **not** open any network listeners or ports. The client only makes
   outbound connections.
4. Never print, log, or commit tokens, passwords, the `grok dial` address,
   or other secrets. The Eden display code (`AB2-CD3`) is meant to be shown
   in the hotline window so the user can compare it with Eden's feed; do
   not put it in git or in a chat log.
5. Eden is the gate: the hotline passes every Eden tool through and Eden's approval prompt and kill switch decide (`grok dial --read-only` restricts to scan tools; see README). Do not
   modify Eden or its approval settings.
6. Do **not** run `grok dial` unless the user has supplied a session
   address in this conversation. Installing and running `grok find-python`
   is enough until then.

## Install, step by step

Work verbosely. If a step fails, show the output and the exit code
before you try a fix.

1. Clone this repo to `%USERPROFILE%\tools\grok-hotline` if it is not
   already there:

   ```bat
   mkdir "%USERPROFILE%\tools" 2>nul
   git clone https://github.com/thriftshoppin/grok-hotline.git "%USERPROFILE%\tools\grok-hotline"
   cd /d "%USERPROFILE%\tools\grok-hotline"
   ```

   If the folder already exists and is this repo, `cd` into it and
   `git pull` instead of cloning again. Do not leave the only copy in a
   random temp directory.

2. From that repo root, run:

   ```bat
   install.cmd
   ```

   `install.cmd` creates `%USERPROFILE%\tools\grok`, copies
   `deck\grok.cmd` and `deck\grok_hotline.py` there, and adds that folder
   to the **user** PATH. It prefers PowerShell's
   `[Environment]::SetEnvironmentVariable` so `setx` cannot truncate the
   PATH. If PowerShell is missing, it falls back to `setx` only after a
   length check. On error it prints the exit code and pauses.

3. Open a **new** terminal (PATH changes do not apply to already-open
   shells) and confirm `grok` is on PATH:

   ```bat
   where grok
   grok help
   ```

   `where grok` should show `%USERPROFILE%\tools\grok\grok.cmd`.

4. Discover Python:

   ```bat
   grok find-python
   ```

5. Confirm it found Python 3.14 and cached the path. Success looks like:

   - a `FOUND` line with a `python.exe` path and `version 3.14.x`
   - `grok: remembered this Python in %USERPROFILE%\tools\grok\python_path.txt`

   Open that cache file and confirm it contains the same `python.exe`
   path. Do not edit the venv the path points at. Prefer this 3.14 over
   any other interpreter that happens to turn up (including 3.13 or a
   WindowsApps stub).

## Verification checklist

Report each item as pass/fail with the command output (redact secrets if
they appear; they should not):

- [ ] Repo is at `%USERPROFILE%\tools\grok-hotline`
- [ ] `install.cmd` exited 0
- [ ] `where grok` in a **new** terminal points at `%USERPROFILE%\tools\grok\grok.cmd`
- [ ] `grok help` runs
- [ ] `grok find-python` found Python 3.14 (not 3.13, and not a WindowsApps stub)
- [ ] `%USERPROFILE%\tools\grok\python_path.txt` exists and matches that 3.14 `python.exe`
- [ ] No packages were installed and the desktop-bridge venv was not modified
- [ ] No ports were opened
- [ ] `grok dial` was **not** run unless the user provided an address

## Troubleshooting

### Python not found, or the wrong Python is found

Python is not on PATH, by design. Do not install Python to fix this.

- Re-run `grok find-python` and read the `FOUND` / `SKIP` lines.
- Skip `WindowsApps` stubs and any interpreter older than 3.8. Prefer
  the 3.14 that already runs the desktop bridge.
- Point at it explicitly, then re-run find-python so the path is cached:

  ```bat
  set "GROK_PYTHON=C:\full\path\to\python.exe"
  grok find-python
  ```

  To persist that for later terminals, set the **user** environment
  variable `GROK_PYTHON` to that `python.exe` (PowerShell:
  `[Environment]::SetEnvironmentVariable('GROK_PYTHON', 'C:\full\path\to\python.exe', 'User')`)
  and open a new terminal.

- If a call is happening and you already have an address from the user,
  you can also pass it just for that run:

  ```bat
  grok dial <address> --python "C:\full\path\to\python.exe"
  ```

### PATH not updated / `grok` is not recognized

- Confirm you opened a **new** terminal after `install.cmd`.
- Confirm `install.cmd` was run from the repo root
  (`%USERPROFILE%\tools\grok-hotline`) so it could see `deck\grok.cmd`.
- Check the user PATH (not the process PATH alone):

  ```bat
  powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('Path','User')"
  ```

  It should include `%USERPROFILE%\tools\grok`.
- Do **not** fix this with `setx PATH "%PATH%;..."`. That can truncate
  the user PATH. Re-run `install.cmd`, or add the folder with PowerShell
  only if it is missing:

  ```bat
  powershell -NoProfile -Command "$dest = $env:USERPROFILE + '\tools\grok'; $p = [Environment]::GetEnvironmentVariable('Path','User'); if ($null -eq $p) { $p = '' }; $parts = @($p -split ';' | Where-Object { $_ -and $_.Trim() -ne '' }); $hit = $false; foreach ($x in $parts) { if ($x.TrimEnd('\').ToLower() -eq $dest.TrimEnd('\').ToLower()) { $hit = $true } }; if (-not $hit) { $new = if ($p.Trim() -eq '') { $dest } else { $p.TrimEnd(';') + ';' + $dest }; [Environment]::SetEnvironmentVariable('Path', $new, 'User'); 'ADDED' } else { 'ALREADY' }"
  ```

  Then open a new terminal.

### Eden is not installed or not found

That is fine. The client uses built-in read-only scan tools (`sysinfo`,
`processes`, `disks`, `network`, `list_dir`, `read_file`, `screenshot`)
when Eden is absent. Do not install Eden to "fix" this. Do not change
an existing Eden install.

### `install.cmd` failed and paused

Read the exit code and the error above the pause. Common causes: not
running from the repo root, or the destination folder could not be
created. Fix the cause and re-run. Do not copy files by hand unless
`install.cmd` cannot run at all. If you must, copy `deck\grok.cmd` and
`deck\grok_hotline.py` into `%USERPROFILE%\tools\grok` and add that
folder to the *user* PATH with PowerShell as above — never with a raw
`setx PATH "%PATH%;..."`.


## Eden pairing code (when this branch is in use)

If Eden is installed, `grok dial` prints a one-time code in the hotline
window (`Eden will show code AB2-CD3 — press y only if it matches`) and
passes it to the launcher as the `EDEN_PAIR_CODE` environment variable. Do
not hide that line. If the user is also running the `pairing-code-gate`
branch of eden-desktop-bridge, tell them to compare that code with the
feed window and press `y` only if they match.

Do **not** modify Eden or check out a branch of it unless the user asked
you to. If they did, a `git fetch` / `git checkout` of the named branch
in `%USERPROFILE%\tools\eden-desktop-bridge` is enough; do not change
its venv, and do not merge to main. Then they need to close Eden's feed
window so the guardian restarts with the new code, re-run `install.cmd`
for this repo, and dial again.

If Eden is still on `main`, the code is printed anyway and then ignored
by Eden; that is fine, do not treat it as a failure.

## What to report back

Tell the user, in plain language:

1. Where the repo was cloned and that `install.cmd` succeeded (or the
   exit code and error if it did not).
2. Whether `grok` is on PATH in a new terminal (`where grok`).
3. The full path of the Python that `grok find-python` found, its
   version (expect 3.14.x), and that it was cached in
   `%USERPROFILE%\tools\grok\python_path.txt`.
4. Whether Eden was detected as present on disk, or that the built-in
   tools will be used if it is not. Do not start a call just to find out.
   If they already dialed and Eden is in use, mention that a pairing code
   was shown in the hotline window (do not repeat the code itself).
5. Anything you had to work around (PATH, a manual `--python` /
   `GROK_PYTHON` path).
6. That the deck is ready for `grok dial <address>` once the user (or
   Grok Bot) provides an address — and that you did not dial yet if no
   address was given.

Do not include tokens, pairing codes, or other secret values in the
report.
