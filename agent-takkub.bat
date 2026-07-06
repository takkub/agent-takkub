@echo off
REM ============================================================
REM  agent-takkub - one-click launcher
REM
REM  Run this file (double-click or `agent-takkub.bat`).
REM  It checks prerequisites, sets up the .venv on first run,
REM  copies projects.json.example if no config exists, and
REM  launches the cockpit.
REM ============================================================

setlocal
set HERE=%~dp0
pushd "%HERE%"

echo.
echo  agent-takkub launcher
echo  =====================
echo.

REM --- 1. Python 3.11+
where python >nul 2>&1
if errorlevel 1 (
  echo  [x] Python is not on PATH.
  echo      Install Python 3.11 or newer from https://www.python.org/downloads/
  echo      Tick "Add Python to PATH" during the installer.
  goto :fail
)
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo  [ok] python %PYVER%

REM --- 2. claude CLI
where claude >nul 2>&1
if errorlevel 1 (
  echo  [x] Claude Code CLI is not on PATH.
  echo      Install:  npm install -g @anthropic-ai/claude-code
  echo      Log in:   claude     -- one-time login
  goto :fail
)
echo  [ok] claude CLI found

REM --- 3. .venv
if not exist ".venv\Scripts\pythonw.exe" (
  echo.
  echo  [..] Creating .venv -- first run only...
  python -m venv .venv
  if errorlevel 1 (
    echo  [x] failed to create .venv
    goto :fail
  )
)

REM --- 4. Deps: install if PyQt6-WebEngine missing
if not exist ".venv\Lib\site-packages\PyQt6\QtWebEngineWidgets.pyd" (
  echo.
  echo  [..] Installing dependencies, takes about 1-2 minutes.
  echo       First run pulls about 150 MB Chromium for PyQt6-WebEngine.
  echo.
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -e .
  if errorlevel 1 (
    echo  [x] pip install failed. See output above.
    goto :fail
  )
)
echo  [ok] dependencies ready

REM --- 4b. Spawn-safe `takkub` launcher (issue #94)
REM  Rust's std (rtk, and any Rust/Node spawner) REFUSES to pass multi-line /
REM  Unicode args to a .bat/.cmd since CVE-2024-24576 -> `rtk takkub assign`
REM  with a long Thai task died with "batch file arguments are invalid".
REM  Fix: drop the pip-built takkub.exe into bin\ (PATHEXT ranks .exe above
REM  .cmd, so `takkub` now resolves to the .exe, which Rust spawns cleanly).
REM  Idempotent; runs every launch so it self-heals after a venv rebuild.
if exist ".venv\Scripts\takkub.exe" copy /Y ".venv\Scripts\takkub.exe" "bin\takkub.exe" >nul

REM --- 5. projects.json
if not exist "projects.json" (
  echo.
  echo  [..] No projects.json yet. Copying from projects.json.example...
  copy /Y "projects.json.example" "projects.json" >nul
  echo.
  echo  ------------------------------------------------------------
  echo   EDIT projects.json BEFORE FIRST USE
  echo  ------------------------------------------------------------
  echo   Open the file that just opened in Notepad and set `paths`
  echo   to your project, e.g.
  echo.
  echo     "web": "C:/Users/you/dev/myapp-web",
  echo     "api": "C:/Users/you/dev/myapp-api"
  echo.
  echo   Save, close Notepad, then re-run this launcher.
  echo  ------------------------------------------------------------
  echo.
  notepad "projects.json"
  goto :end
)
echo  [ok] projects.json found

REM --- 6. Launch detached
echo.
echo  [..] launching cockpit...
start "" ".venv\Scripts\pythonw.exe" -m agent_takkub
echo  [ok] cockpit started. Look for the agent-takkub window.
goto :end

:fail
echo.
echo  Setup did not complete. Fix the issue above, then re-run this file.
pause
exit /b 1

:end
popd
endlocal
exit /b 0
