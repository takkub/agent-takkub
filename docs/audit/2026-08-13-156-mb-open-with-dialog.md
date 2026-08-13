# Root Cause Analysis and Audit Fix Report for Bug #156

**Issue:** Bug #156 — Spawning a `gemini` / `agy` pane on Windows caused the OS dialog *"Select an app to open 'mb'"* to pop up every time.
**Date:** 2026-08-13
**Branch:** `wt/backend-1786586053`

---

## 1. Ground-Truth Root Cause Analysis

Through empirical trace and Win32 API verification (`SearchPathW` / `ShellExecuteW` via ctypes):

1. **Extensionless POSIX shims on Windows `PATH`:**
   - Global npm installations (`npm install -g @runablehq/mini-browser`) and POSIX tool installers drop an extensionless POSIX shell script named `mb` (`#!/bin/sh`) alongside `mb.cmd` into `%APPDATA%\npm\mb` and `~/.local/bin/mb`.
   - On Windows, extensionless files have **no registered file verb or default application association** in the Windows Registry.

2. **Win32 Executable Resolution Behavior (`SearchPathW` / `exec.LookPath`):**
   - When a native compiled Windows binary (such as `agy.exe`, the Antigravity CLI written in Go) boots up in a spawned `gemini` pane, it auto-discovers `AGENTS.md` and initializes or probes tools mentioned in `AGENTS.md` (e.g. `mb`).
   - Win32 `SearchPathW(NULL, "mb", NULL)` and Go `exec.LookPath("mb")` search directories on `PATH` for exact literal matches for `"mb"` BEFORE appending `PATHEXT` extensions (`.cmd`, `.exe`).
   - Win32 `SearchPathW` matched the extensionless POSIX script `C:\Users\monch\.local\bin\mb` (or `%APPDATA%\npm\mb`) as an exact match for `"mb"`.

3. **OS Dialog Trigger (`ShellExecute`):**
   - When `agy.exe` or Windows Shell attempted to invoke `C:\Users\monch\.local\bin\mb` via `ShellExecute` or `CreateProcess` without an explicit `.cmd` extension, Windows detected an extensionless file with no registered file type association.
   - Windows OS immediately popped up the native GUI modal: **"Select an app to open 'mb'"**.

---

## 2. Summary of Fix Implementation

1. **Win32 Shim Sanitization (`src/agent_takkub/_win_console.py`):**
   - Added `sanitize_win32_mb_shims()`.
   - On Windows (`sys.platform == "win32"`), it detects extensionless POSIX scripts named `mb` under `%APPDATA%\npm\mb`, `~/.local/bin/mb`, or `CLI_BIN_DIR/mb`.
   - When `mb.cmd` exists on the system, it renames the extensionless POSIX script to `mb.sh` (preventing Win32 `SearchPathW` from matching literal extensionless `"mb"`).

2. **Pane Environment & PATH Sanitization (`src/agent_takkub/pane_env.py`):**
   - Added `_apply_win32_path_sanitization(env)` to `_build_pane_env()` and `_build_lead_env()`.
   - Automatically sanitizes extensionless shims and reorders `PATH` so `%APPDATA%\npm` (where `mb.cmd` lives) is prioritized over `~/.local/bin`.

3. **Doctor Health Check (`src/agent_takkub/doctor.py`):**
   - Updated `check_mini_browser()` to invoke `sanitize_win32_mb_shims()` on Windows so `takkub doctor` and `takkub doctor --fix` proactively clean any extensionless POSIX shims on Windows.

4. **Targeted Tests (`tests/test_native_chrome.py`):**
   - Added `test_sanitize_win32_mb_shims_renames_extensionless_mb` to verify that extensionless `mb` POSIX shims are renamed to `mb.sh` when `mb.cmd` exists.

---

## 3. Verification

- All 192 targeted unit tests passed cleanly (`pytest -o pythonpath=src tests/test_doctor.py tests/test_native_chrome.py tests/test_pane_guard.py`).
- Empirical Win32 API trace confirmed `SearchPathW` and `shutil.which('mb')` resolve cleanly to `mb.cmd` with zero Open-With dialogs.
