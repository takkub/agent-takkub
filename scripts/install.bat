@echo off
REM ─────────────────────────────────────────────────────────────
REM agent-takkub installer wrapper (.bat) — double-click friendly.
REM
REM Why this exists:
REM   PowerShell's default execution policy on Windows can block
REM   running `install.ps1` directly. This wrapper bypasses the
REM   policy for this one invocation only (no system change) and
REM   passes through any args you give it (-Update, -SkipLogin,
REM   -VaultDir "...", etc.).
REM
REM Usage:
REM   install.bat                  ← lazy mode (skip what's present)
REM   install.bat -Update          ← upgrade everything
REM   install.bat -SkipLogin       ← skip claude/codex login prompts
REM   install.bat -VaultDir ""     ← skip Obsidian vault skeleton
REM
REM See `install.ps1` for the full description of phases / flags.
REM ─────────────────────────────────────────────────────────────

setlocal
set "SCRIPT=%~dp0install.ps1"

if not exist "%SCRIPT%" (
    echo [FAIL] install.ps1 not found next to this .bat
    echo        expected at: %SCRIPT%
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" %*
set "RC=%ERRORLEVEL%"

endlocal & exit /b %RC%
