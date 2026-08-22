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
REM of it then fails silently (empty output, no diagnostic). A real venv
REM python.exe is comfortably >90KB; catch anything wildly smaller before
REM ever spawning it.
for %%A in ("%PY%") do set PYSIZE=%%~zA
if %PYSIZE% LSS 40960 (
  echo [takkub] %PY% looks broken ^(only %PYSIZE% byte^(s^) — a real interpreter is much larger^). 1>&2
  echo Something outside agent-takkub overwrote this file. Recreate the venv: 1>&2
  echo   rmdir /s /q "%REPO%\.venv" ^&^& scripts\run.bat 1>&2
  exit /b 1
)

"%PY%" -m agent_takkub.cli %*
exit /b %errorlevel%
