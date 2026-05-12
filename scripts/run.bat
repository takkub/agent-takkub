@echo off
REM Launch agent-takkub cockpit using the local .venv (no console window).

setlocal
set HERE=%~dp0
pushd "%HERE%.."

REM Create venv only if missing entirely. Install deps only if PyQt6 missing.
REM This avoids re-running setup when an orphan process locks a venv file.
if not exist ".venv\Scripts\pythonw.exe" (
  echo .venv not found. Creating fresh...
  python -m venv .venv
)

if not exist ".venv\Lib\site-packages\PyQt6" (
  echo Installing dependencies, one-off, takes about 1 min...
  .venv\Scripts\python.exe -m pip install --upgrade pip
  .venv\Scripts\python.exe -m pip install -e .
)

if not exist ".venv\Scripts\pythonw.exe" (
  echo .venv setup failed. Inspect output above, then delete .venv and re-run.
  popd
  exit /b 1
)

REM `start ""` detaches the process so this batch and its cmd.exe host
REM exit immediately. `pythonw.exe` is console-less so no window pops up.
start "" ".venv\Scripts\pythonw.exe" -m agent_takkub
popd
exit /b 0
