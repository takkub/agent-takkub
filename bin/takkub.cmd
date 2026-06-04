@echo off
REM `takkub` CLI shim — finds the project venv and runs the CLI module.

setlocal
set HERE=%~dp0
set REPO=%HERE%..

if not exist "%REPO%\.venv\Scripts\python.exe" (
  echo agent-takkub .venv not found at %REPO%\.venv 1>&2
  echo Run scripts\run.bat once to set it up. 1>&2
  exit /b 1
)

"%REPO%\.venv\Scripts\python.exe" -m agent_takkub.cli %*
exit /b %errorlevel%
