@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Grok Hotline
rem Self-contained launcher. Does not install packages or change any bridge venv.
rem Finds the Python that already runs the Claude desktop bridge (often not on PATH).

set "HERE=%~dp0"
set "CACHE_DIR=%USERPROFILE%\tools\grok"
set "CACHE=%CACHE_DIR%\python_path.txt"
set "PY="
set "PY_SOURCE="
set "CMD_NAME=%~1"

rem Pull --python PATH or GROK_PYTHON without dropping any args (they are forwarded as %*).
set "PYTHON_OVERRIDE="
set "prev="
for %%A in (%*) do (
  if /I "!prev!"=="--python" set "PYTHON_OVERRIDE=%%~A"
  set "prev=%%~A"
)
if defined GROK_PYTHON if not defined PYTHON_OVERRIDE set "PYTHON_OVERRIDE=%GROK_PYTHON%"

if /I "%CMD_NAME%"=="help" goto :HELP_MAYBE
if /I "%CMD_NAME%"=="-h" goto :HELP_MAYBE
if /I "%CMD_NAME%"=="--help" goto :HELP_MAYBE
if /I "%CMD_NAME%"=="find-python" goto :FIND_PYTHON
if /I "%CMD_NAME%"=="find_python" goto :FIND_PYTHON

goto :NEED_PY

:HELP_MAYBE
rem If we can find Python, use the script's help text. Otherwise print a short copy here.
call :RESOLVE_PY
if errorlevel 1 goto :HELP_TEXT
goto :RUN

:NEED_PY
call :RESOLVE_PY
if errorlevel 1 goto :NO_PYTHON
goto :RUN

:RUN
if not exist "%HERE%grok_hotline.py" (
  echo grok: grok_hotline.py is missing next to grok.cmd
  echo Exit code: 1
  pause
  exit /b 1
)
echo grok: using Python from !PY_SOURCE!
echo grok: !PY!
"%PY%" "%HERE%grok_hotline.py" %*
set "EC=!ERRORLEVEL!"
exit /b !EC!

:HELP_TEXT
echo Grok Hotline
echo.
echo   grok dial ^<address^>              Call in and start a read-only scan session
echo   grok dial ^<address^> --python P   Use this python.exe (not on PATH is fine)
echo   grok find-python                 Search this PC for a Python we can use
echo   grok hangup                      Hang up a call started by grok dial
echo   grok help                        Show this help
echo.
echo Address looks like:  some-words.trycloudflare.com/X7K2P9
echo.
echo This kit is stdlib-only. It will use Eden if it is installed, otherwise
echo its built-in scan tools (sysinfo, processes, disks, network, files, screenshot^).
exit /b 0

:NO_PYTHON
echo.
echo grok: Could not find a Python 3.8+ interpreter.
echo Python is not on PATH, so grok looked next to the Claude desktop bridge
echo and in common folders. Nothing usable was found.
echo.
echo Try one of these:
echo   1. grok find-python
echo      If it prints a python.exe path, run:
echo      grok dial ADDRESS --python "C:\full\path\to\python.exe"
echo   2. Set the user environment variable GROK_PYTHON to that python.exe
echo   3. Download the official "Windows embeddable package (ZIP)" from
echo      https://www.python.org/downloads/windows/
echo      and unzip it into this folder so python.exe sits next to grok.cmd
echo.
echo Exit code: 1
pause
exit /b 1

:RESOLVE_PY
if defined PYTHON_OVERRIDE (
  call :TRY_ONE "%PYTHON_OVERRIDE%" "command/env override"
  if not "!PY!"=="" call :CACHE_PY
  if not "!PY!"=="" exit /b 0
  echo grok: --python / GROK_PYTHON is not a usable Python 3.8+: %PYTHON_OVERRIDE%
)
if defined EDEN_PYTHON (
  call :TRY_ONE "%EDEN_PYTHON%" "EDEN_PYTHON"
  if not "!PY!"=="" call :CACHE_PY
  if not "!PY!"=="" exit /b 0
)
if exist "%CACHE%" (
  set /p CACHED=<"%CACHE%"
  if exist "!CACHED!" (
    call :TRY_ONE "!CACHED!" "cached path in %CACHE%"
    if not "!PY!"=="" exit /b 0
    echo grok: cached path is stale, searching again
  )
)
call :TRY_ONE "%HERE%python.exe" "next to grok.cmd"
if not "!PY!"=="" call :CACHE_PY
if not "!PY!"=="" exit /b 0
call :TRY_ONE "%HERE%runtime\python.exe" "runtime folder next to grok.cmd"
if not "!PY!"=="" call :CACHE_PY
if not "!PY!"=="" exit /b 0

rem python / py on PATH (often missing on this deck)
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; raise SystemExit(0 if sys.version_info>=(3,8) else 1)" >nul 2>nul
  if not errorlevel 1 (
    rem py launcher found, but we want a real python.exe path for the cache
    for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do (
      call :TRY_ONE "%%P" "py -3 launcher"
    )
    if not "!PY!"=="" call :CACHE_PY
    if not "!PY!"=="" exit /b 0
  )
)
for /f "delims=" %%P in ('where python 2^>nul') do (
  echo %%P | find /I "WindowsApps" >nul
  if errorlevel 1 (
    call :TRY_ONE "%%P" "PATH"
    if not "!PY!"=="" call :CACHE_PY
    if not "!PY!"=="" exit /b 0
  )
)

call :SEARCH_COMMON 1
if not "!PY!"=="" call :CACHE_PY
if not "!PY!"=="" exit /b 0
exit /b 1

:FIND_PYTHON
echo grok: searching this PC for python.exe (will not change any install)
echo.
set "FOUND_ANY="
if defined PYTHON_OVERRIDE (
  echo -- override --
  call :PRINT_ONE "%PYTHON_OVERRIDE%" "GROK_PYTHON / --python"
)
if exist "%CACHE%" (
  set /p CACHED=<"%CACHE%"
  echo -- cache %CACHE% --
  call :PRINT_ONE "!CACHED!" "cached"
)
echo -- next to grok.cmd --
call :PRINT_ONE "%HERE%python.exe" "next to grok.cmd"
call :PRINT_ONE "%HERE%runtime\python.exe" "runtime folder"
echo -- common folders --
call :SEARCH_COMMON 0
if not defined FOUND_ANY (
  echo.
  echo No usable python.exe found.
  echo See: https://www.python.org/downloads/windows/  ^(embeddable zip is enough^)
  echo Exit code: 1
  pause
  exit /b 1
)
echo.
echo To use a listed path:  grok dial ADDRESS --python "C:\path\to\python.exe"
echo Or set GROK_PYTHON to that path. The first usable path is cached at:
echo   %CACHE%
exit /b 0

:SEARCH_COMMON
rem %1=1 means stop at first usable (and set PY). %1=0 means print all.
set "STOP=%~1"

rem %USERPROFILE%\tools\*\.venv|venv\Scripts\python.exe
if exist "%USERPROFILE%\tools" (
  for /d %%D in ("%USERPROFILE%\tools\*") do (
    call :CONSIDER "%%~D\.venv\Scripts\python.exe" "%%~D\.venv" %STOP%
    if "%STOP%"=="1" if not "!PY!"=="" exit /b 0
    call :CONSIDER "%%~D\venv\Scripts\python.exe" "%%~D\venv" %STOP%
    if "%STOP%"=="1" if not "!PY!"=="" exit /b 0
    call :CONSIDER "%%~D\Scripts\python.exe" "%%~D\Scripts" %STOP%
    if "%STOP%"=="1" if not "!PY!"=="" exit /b 0
    call :CONSIDER "%%~D\python.exe" "%%~D" %STOP%
    if "%STOP%"=="1" if not "!PY!"=="" exit /b 0
  )
)

rem Known default Eden / tools paths
call :CONSIDER "%USERPROFILE%\tools\eden-desktop-bridge\.venv\Scripts\python.exe" "default Eden venv" %STOP%
if "%STOP%"=="1" if not "!PY!"=="" exit /b 0

rem %USERPROFILE%\*bridge*
for /d %%D in ("%USERPROFILE%\*bridge*") do (
  call :WALK_BRIDGE "%%~D" %STOP%
  if "%STOP%"=="1" if not "!PY!"=="" exit /b 0
)
for /d %%D in ("%USERPROFILE%\tools\*bridge*") do (
  call :WALK_BRIDGE "%%~D" %STOP%
  if "%STOP%"=="1" if not "!PY!"=="" exit /b 0
)

rem Official CPython installs
if exist "%LOCALAPPDATA%\Programs\Python" (
  for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    call :CONSIDER "%%~D\python.exe" "%%~D" %STOP%
    if "%STOP%"=="1" if not "!PY!"=="" exit /b 0
  )
)

rem Documents\Codex worktrees (historical bridge checkouts)
if exist "%USERPROFILE%\Documents\Codex" (
  echo grok: searching %%USERPROFILE%%\Documents\Codex
  call :WHERE_ROOT "%USERPROFILE%\Documents\Codex" %STOP%
  if "%STOP%"=="1" if not "!PY!"=="" exit /b 0
)

rem Limited where /r roots (not the whole profile yet)
if exist "%USERPROFILE%\tools" (
  echo grok: searching %%USERPROFILE%%\tools
  call :WHERE_ROOT "%USERPROFILE%\tools" %STOP%
  if "%STOP%"=="1" if not "!PY!"=="" exit /b 0
)
if exist "%LOCALAPPDATA%\Programs" (
  echo grok: searching %%LOCALAPPDATA%%\Programs
  call :WHERE_ROOT "%LOCALAPPDATA%\Programs" %STOP%
  if "%STOP%"=="1" if not "!PY!"=="" exit /b 0
)

rem Last resort: user profile. Can take a minute. Skip if we already found some in list mode? Still run in resolve mode.
echo grok: last-resort search under %%USERPROFILE%% (may take a minute)
call :WHERE_ROOT "%USERPROFILE%" %STOP%
if "%STOP%"=="1" if not "!PY!"=="" exit /b 0
exit /b 0

:WALK_BRIDGE
rem Walk a bridge-named folder a couple of levels for python.exe / venv
call :CONSIDER "%~1\python.exe" "%~1" %2
if "%2"=="1" if not "!PY!"=="" exit /b 0
call :CONSIDER "%~1\.venv\Scripts\python.exe" "%~1\.venv" %2
if "%2"=="1" if not "!PY!"=="" exit /b 0
call :CONSIDER "%~1\venv\Scripts\python.exe" "%~1\venv" %2
if "%2"=="1" if not "!PY!"=="" exit /b 0
call :CONSIDER "%~1\Scripts\python.exe" "%~1\Scripts" %2
if "%2"=="1" if not "!PY!"=="" exit /b 0
for /d %%E in ("%~1\*") do (
  call :CONSIDER "%%~E\python.exe" "%%~E" %2
  if "%2"=="1" if not "!PY!"=="" exit /b 0
  call :CONSIDER "%%~E\.venv\Scripts\python.exe" "%%~E\.venv" %2
  if "%2"=="1" if not "!PY!"=="" exit /b 0
  call :CONSIDER "%%~E\venv\Scripts\python.exe" "%%~E\venv" %2
  if "%2"=="1" if not "!PY!"=="" exit /b 0
  call :CONSIDER "%%~E\Scripts\python.exe" "%%~E\Scripts" %2
  if "%2"=="1" if not "!PY!"=="" exit /b 0
)
exit /b 0

:WHERE_ROOT
rem %1=root %2=stop-at-first
if not exist "%~1" exit /b 0
for /f "delims=" %%P in ('where /r "%~1" python.exe 2^>nul') do (
  echo %%P | find /I "\WindowsApps\" >nul
  if errorlevel 1 (
    echo %%P | find /I "\Temp\" >nul
    if errorlevel 1 (
      echo %%P | find /I "\node_modules\" >nul
      if errorlevel 1 (
        echo %%P | find /I "\__pycache__\" >nul
        if errorlevel 1 (
          call :CONSIDER "%%P" "search in %~1" %2
          if "%2"=="1" if not "!PY!"=="" exit /b 0
        )
      )
    )
  )
)
exit /b 0

:CONSIDER
rem %1=path %2=label %3=stop flag (1 sets PY and returns)
if not exist "%~1" exit /b 0
if "%~3"=="1" (
  call :TRY_ONE "%~1" "%~2"
  exit /b 0
)
call :PRINT_ONE "%~1" "%~2"
exit /b 0

:TRY_ONE
if not exist "%~1" exit /b 1
echo "%~1" | find /I "\WindowsApps\" >nul
if not errorlevel 1 exit /b 1
"%~1" -c "import sys; raise SystemExit(0 if sys.version_info>=(3,8) else 1)" >nul 2>&1
if errorlevel 1 (
  echo grok: skipped (not Python 3.8+): %~1
  exit /b 1
)
set "PY=%~1"
set "PY_SOURCE=%~2"
exit /b 0

:PRINT_ONE
if not exist "%~1" exit /b 0
echo "%~1" | find /I "\WindowsApps\" >nul
if not errorlevel 1 exit /b 0
set "VER="
for /f "delims=" %%V in ('"%~1" -c "import sys; print(sys.version.split()[0])" 2^>nul') do set "VER=%%V"
if not defined VER (
  echo   SKIP  %~1  ^(%~2, not runnable^)
  exit /b 0
)
echo   FOUND %~1
echo         version !VER!  ^(%~2^)
set "FOUND_ANY=1"
exit /b 0

:CACHE_PY
if not defined PY exit /b 0
if not exist "%CACHE_DIR%" mkdir "%CACHE_DIR%" 2>nul
> "%CACHE%" echo %PY%
echo grok: remembered this Python in %CACHE%
exit /b 0
