@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Grok Hotline installer
cd /d "%~dp0"
set "EXITCODE=0"

echo ============================================
echo  Grok Hotline installer
echo ============================================
echo.

set "SRC_CMD=%~dp0deck\grok.cmd"
set "SRC_PY=%~dp0deck\grok_hotline.py"
set "DEST=%USERPROFILE%\tools\grok"

echo [info] Repo root:  %CD%
echo [info] Destination: %DEST%
echo.

if not exist "%SRC_CMD%" (
    echo [error] Missing "%SRC_CMD%"
    echo         Run install.cmd from the root of the cloned grok-hotline repo.
    set "EXITCODE=1"
    goto :fail
)
if not exist "%SRC_PY%" (
    echo [error] Missing "%SRC_PY%"
    echo         Run install.cmd from the root of the cloned grok-hotline repo.
    set "EXITCODE=1"
    goto :fail
)

echo [1/3] Creating install folder
if not exist "%DEST%" (
    mkdir "%DEST%"
    if errorlevel 1 (
        echo [error] Could not create "%DEST%"
        set "EXITCODE=1"
        goto :fail
    )
    echo       Created "%DEST%"
) else (
    echo       Already exists.
)
echo.

echo [2/3] Copying files
copy /Y "%SRC_CMD%" "%DEST%\grok.cmd"
if errorlevel 1 (
    echo [error] Failed to copy grok.cmd
    set "EXITCODE=1"
    goto :fail
)
copy /Y "%SRC_PY%" "%DEST%\grok_hotline.py"
if errorlevel 1 (
    echo [error] Failed to copy grok_hotline.py
    set "EXITCODE=1"
    goto :fail
)
echo       Copy complete.
echo.

echo [3/3] Adding destination to the user PATH
echo       ^(user PATH only; the system PATH is left alone^)
where powershell >nul 2>&1
if errorlevel 1 (
    echo       PowerShell not found. Falling back to setx with a length check.
    goto :setx_fallback
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference = 'Stop';" ^
    "$dest = $env:DEST;" ^
    "if ([string]::IsNullOrWhiteSpace($dest)) { $dest = [Environment]::GetFolderPath('UserProfile') + '\tools\grok' };" ^
    "$userPath = [Environment]::GetEnvironmentVariable('Path', 'User');" ^
    "if ($null -eq $userPath) { $userPath = '' };" ^
    "function Normalize([string]$p) {" ^
    "  if ([string]::IsNullOrWhiteSpace($p)) { return '' };" ^
    "  try { return [IO.Path]::GetFullPath($p.Trim().TrimEnd('\')).ToLowerInvariant() } catch { return $p.Trim().TrimEnd('\').ToLowerInvariant() }" ^
    "};" ^
    "$destN = Normalize $dest;" ^
    "$already = $false;" ^
    "foreach ($part in ($userPath -split ';')) {" ^
    "  if ((Normalize $part) -eq $destN) { $already = $true; break }" ^
    "};" ^
    "if ($already) { Write-Output 'ALREADY'; exit 0 };" ^
    "if ([string]::IsNullOrWhiteSpace($userPath)) { $newPath = $dest } else { $newPath = $userPath.TrimEnd(';') + ';' + $dest };" ^
    "[Environment]::SetEnvironmentVariable('Path', $newPath, 'User');" ^
    "$readBack = [Environment]::GetEnvironmentVariable('Path', 'User');" ^
    "if ($null -eq $readBack) { Write-Output 'VERIFY_FAIL'; exit 2 };" ^
    "$ok = $false;" ^
    "foreach ($part in ($readBack -split ';')) { if ((Normalize $part) -eq $destN) { $ok = $true; break } };" ^
    "if (-not $ok) { Write-Output 'VERIFY_FAIL'; exit 2 };" ^
    "Write-Output 'ADDED'; exit 0"

set "PS_EC=!errorlevel!"
if "!PS_EC!"=="0" (
    echo       User PATH updated ^(or already listed^).
    goto :path_done
)
echo [error] PowerShell PATH update failed, exit code !PS_EC!. Trying setx fallback.
goto :setx_fallback

:setx_fallback
set "USERPATH="
for /f "tokens=2,*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "USERPATH=%%B"
if defined USERPATH (
    echo !USERPATH! | findstr /I /C:"%DEST%" >nul
    if not errorlevel 1 (
        echo       Already on the user PATH.
        goto :path_done
    )
    set "NEWPATH=!USERPATH!;%DEST%"
) else (
    set "NEWPATH=%DEST%"
)

if not "!NEWPATH:~1024,1!"=="" (
    echo [error] User PATH plus the new entry would exceed the 1024-character setx limit.
    echo         Install the files already copied to "%DEST%", then add that folder
    echo         to your user PATH from Windows Settings ^> Environment Variables.
    set "EXITCODE=3"
    goto :fail
)

echo       Writing user PATH with setx...
setx PATH "!NEWPATH!"
if errorlevel 1 (
    echo [error] setx failed.
    set "EXITCODE=1"
    goto :fail
)

set "READBACK="
for /f "tokens=2,*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "READBACK=%%B"
echo !READBACK! | findstr /I /C:"%DEST%" >nul
if errorlevel 1 (
    echo [error] setx returned success, but "%DEST%" is not in the user PATH.
    echo         The value may have been truncated. Add the folder from
    echo         Windows Settings ^> Environment Variables.
    set "EXITCODE=3"
    goto :fail
)
echo       User PATH updated with setx and verified.

:path_done
rem Make grok available in THIS shell too (other already-open shells still need a restart).
echo !PATH! | findstr /I /C:"%DEST%" >nul
if errorlevel 1 set "PATH=%DEST%;%PATH%"
echo.

echo ============================================
echo  Install finished.
echo ============================================
echo.
echo Open a NEW terminal, then run:
echo   grok find-python
echo   grok dial ^<address^>
echo.
echo The address comes from Grok Bot for that session.
echo Exit code: 0
exit /b 0

:fail
echo.
echo ============================================
echo  Install failed. Exit code: !EXITCODE!
echo ============================================
echo.
pause
exit /b !EXITCODE!
