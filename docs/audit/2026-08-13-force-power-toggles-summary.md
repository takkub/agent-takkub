# Audit Summary: Forcing Power Modes for Toggles (#104)

**Date:** 2026-08-13
**Role:** backend

## Objective
Remove 3 toggle controls from the UI and enforce their "power" settings permanently (ignoring persisted state files while preserving backend plumbing to avoid unnecessary diffs):
1. **Multi/1:1 Exec Mode:** Removed chip from UI; forced `is_parallel()` to return `True` and `current()` to return `PARALLEL`.
2. **RTK On/Off Toggle:** Removed toggle button from UI; forced `rtk_hook_enabled()` and `is_rtk_installed()` to return `True`.
3. **Auto-Resume Toggle:** Removed chip from UI; forced `is_enabled()` and `current()` to return `True`.

---

## Files Modified & Key Changes

### 1. `src/agent_takkub/exec_mode.py`
- Updated `current()` to return `"parallel"` (PARALLEL) directly.
- Updated `is_parallel()` to return `True` directly.
- Kept `set_current()` and path functions intact for persistence plumbing compatibility.

### 2. `src/agent_takkub/auto_resume.py`
- Updated `current()` to return `True` directly.
- Updated `is_enabled()` to return `True` directly.
- Kept `set_enabled()` and path functions intact.

### 3. `src/agent_takkub/rtk_helper.py`
- Updated `rtk_hook_enabled()` to return `True` directly.
- Kept `install_rtk()`, `uninstall_rtk()`, and path helper functions intact.

### 4. `src/agent_takkub/status_header.py`
- Removed creation and layout placement of `_btn_install_rtk`, `_chip_exec_mode`, and `_chip_auto_resume`.
- Disconnected `execModeChanged` and `autoResumeChanged` signals.
- Made `_refresh_rtk_button()` a no-op method for safety against external callers.

### 5. `src/agent_takkub/user_actions.py` & `src/agent_takkub/update_panel.py`
- Converted chip click / state change handlers (`_on_exec_mode_chip_clicked`, `_on_exec_mode_changed`, `_on_auto_resume_chip_clicked`, `_on_auto_resume_changed`, `_on_install_rtk_clicked`) into safe no-ops.

### 6. `src/agent_takkub/main_window.py`
- Removed execution mode step from `_build_tutorial_steps` and renumbered remaining steps.

### 7. `CLAUDE.md`
- Updated execution mode documentation section to specify that execution mode is always PARALLEL (Multi mode).

---

## Verification
Targeted tests updated and verified:
- `tests/test_exec_mode.py`
- `tests/test_auto_resume.py`
- `tests/test_rtk_helper.py`
- `tests/test_hook_wiring.py`

**Result:** 46 passed in 4.38s.
