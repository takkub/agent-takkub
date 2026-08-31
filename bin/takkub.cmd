@echo off
REM `takkub` CLI shim — finds the project venv and runs the CLI module.

setlocal
set HERE=%~dp0
set REPO=%HERE%..

set PY=%REPO%\.venv\Scripts\python.exe

if not exist "%PY%" (
  echo agent-takkub .venv not found at %REPO%\.venv 1>&2
  echo Run scripts\run.bat once to set it up. 1>&2
  exit /b 1
)

REM #341: a python.exe that EXISTS but was overwritten/truncated by something
REM outside agent-takkub must never be launched — every command built on top
REM of it then fails silently (empty output, no diagnostic). This mirrors
REM npm/scripts/lib.js's pythonExecutableProblem(): a small size floor that
REM only rules out truncation, plus a check for the 'MZ' PE header — a
REM Windows venv python.exe is always a real PE executable, so unlike the
REM POSIX side (#446) there is no legitimate small-stub case here to spare.
for %%A in ("%PY%") do set PYSIZE=%%~zA
if %PYSIZE% LSS 1024 (
  echo [takkub] %PY% looks broken ^(only %PYSIZE% byte^(s^) — too small to be a real interpreter^). 1>&2
  echo Something outside agent-takkub overwrote this file. Recreate the venv: 1>&2
  echo   rmdir /s /q "%REPO%\.venv" ^&^& scripts\run.bat 1>&2
  exit /b 1
)

set PYMAGIC=
for /f "usebackq" %%M in (`powershell -NoProfile -Command "$fs=[System.IO.File]::OpenRead('%PY%'); $b=New-Object byte[] 2; $fs.Read($b,0,2)|Out-Null; $fs.Close(); [System.Text.Encoding]::ASCII.GetString($b)"`) do set PYMAGIC=%%M
if not "%PYMAGIC%"=="MZ" (
  echo [takkub] %PY% looks broken ^(header is not a valid Windows PE 'MZ' executable^). 1>&2
  echo Something outside agent-takkub overwrote this file. Recreate the venv: 1>&2
  echo   rmdir /s /q "%REPO%\.venv" ^&^& scripts\run.bat 1>&2
  exit /b 1
)

"%PY%" -m agent_takkub.cli %*
exit /b %errorlevel%
