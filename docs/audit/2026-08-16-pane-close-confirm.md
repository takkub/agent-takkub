# Pane-close confirmation (2026-08-16)

**Issue:** clicking × on a pane closed it instantly with no way to catch a
misclick — a working teammate's session (and any uncommitted worktree work)
could vanish with one accidental click.

## What changed

- **`Orchestrator.confirm_manual_pane_close(pane, role_name, project) -> bool`**
  (`orchestrator.py`) — new public method, the single dialog point shared by
  both manual close entry points. Uses `cockpit_theme.themed_message_box`
  (gold/dark themed `QMessageBox`, already established by `danger_zone.py`)
  with `Cancel | Ok`, default button = **Cancel** (Enter does not close).
  - Lead → always returns `True` with no dialog (`close()` already no-ops
    Lead unless `force=True`; a dialog on a guaranteed no-op would just be
    confusing).
  - Message tier 1 (spec #3): pane `state == "working"` → strong wording
    ("กำลังทำงานอยู่ — ปิดตอนนี้จะตัดงานที่กำลังรันทิ้งทันที"). Any other
    state → plain "ปิด pane 'X'?".
  - Worktree tier (spec #2): looks up `Orchestrator._pane_state["{project}::{role}"].worktree`
    (same dict `done()`'s digest-facts build reads) and, if present, calls
    `WorktreeManager.is_dirty()` / `.uncommitted_count()` / `.commit_count()`
    (read-only calls — `worktree_manager.py` itself was not touched) to
    append "⚠ N ไฟล์ที่ยังไม่ commit" and/or "⚠ N commit ที่ยังไม่ได้ merge
    กลับ" lines when applicable. Silently skips this section on any git
    read error (`_log_event("confirm_close_worktree_check_error", ...)`,
    no crash, no blocking).

- **`_on_pane_close_clicked`** (`orchestrator.py`, wired from
  `AgentPane._btn_close.clicked` → `closeRequested` in `spawn_engine.register_pane`)
  now resolves the actual sender pane + its real project (via `sender()` +
  `_project_ns_for_pane`, the same fix pattern `_on_pane_input` already used
  for multi-tab correctness), calls `confirm_manual_pane_close`, and returns
  without closing on Cancel. The actual `self.close(role_name)` call is
  otherwise byte-for-byte what it was before this change.

- **`main_window._wire_project_tab`** — the tab-bar × (`ProjectTab.paneCloseRequested`)
  now routes through a new `_on_tab_pane_close_requested(role, project, tab)`
  slot that calls the same `orch.confirm_manual_pane_close(...)` before
  calling `orch.close(role, project=project)`. Previously this was a direct
  lambda straight to `orch.close`.

## Close-path audit — who asks, who doesn't

| Entry point | Code path | Confirms? | Why |
|---|---|---|---|
| Pane-header × (in-tab) | `AgentPane._btn_close` → `closeRequested` → `Orchestrator._on_pane_close_clicked` | ✅ new gate | User misclick target #1 (spec #1/#2/#3) |
| Tab-bar × | `ProjectTab._on_pane_tab_close` → `paneCloseRequested` → `main_window._on_tab_pane_close_requested` | ✅ new gate | Same misclick surface, different widget |
| `takkub close --role` (CLI) | `cli_server.py:552` → `self._orch.close(...)` direct | ❌ unchanged | Automation — must never wait on a click |
| `takkub close --all` (CLI) | `cli_server.py:554` → `close_all_teammates(...)` direct | ❌ unchanged | Automation |
| Auto-close 2.5s after `takkub done` | `lead_inbox.py:1699` → `self.close(...)` direct | ❌ unchanged | Automation — this is the documented "pane closes 2.5s after done" flow |
| Stuck-pane watchdog recovery-close | `orchestrator.py:5681` → `self.close(..., suppress_pipeline=True)` direct | ❌ unchanged | Automated recovery cycle, not a user action |
| Project restart (`_restart_lead_for_active_project`) | `main_window.py:1293/1295` → `close_all_teammates()` + `close(LEAD, force=True)` direct | ❌ unchanged | Different user action (project switch), already understood as "everything in this project restarts"; not the single-pane-misclick case this issue targets |
| "End Session" button | `user_actions.py:235` → `close_all_teammates(...)` direct | ❌ unchanged | Already has its own explicit note-entry confirm flow, a deliberate bulk action, not an accidental single click |
| Close whole project tab (sidebar) | `main_window.py` tab-close handler → `close_all_teammates()` + `close(LEAD, force=True)` direct | ❌ unchanged (has its own separate `QMessageBox.question` already) | Pre-existing project-level confirm covers this; adding the per-pane gate here too would double-prompt |
| Cockpit shutdown (`closeEvent` / `aboutToQuit`) | `main_window.closeEvent` (own multi-tab confirm) → teardown; `app.py`'s `aboutToQuit` → `_kill_all` | ❌ unchanged | Has its own multi-tab confirm when >1 project tab is open; `aboutToQuit`/kill-signal teardown must never block on a dialog |
| Remote/mobile `close_project` API | `remote/api.py` (`confirm=False` path) | ❌ unchanged | Phone confirms client-side before calling in — not touched (file is in the do-not-touch list anyway) |

**`orchestrator.close()` and `close_all_teammates()` themselves were not
modified** — every automated caller above keeps calling them directly, so
none of them can ever block on a human click.

## Tests

`tests/test_pane_close_confirm.py` (11 tests, targeted run only, via repo
`.venv`):
- Lead never dialogs.
- Ok → proceed, Cancel → abort.
- Default button is Cancel (Enter-safe).
- Working vs. idle wording differs (and idle text has no "กำลังทำงานอยู่").
- Dirty worktree appends the file-count warning with branch name; clean
  worktree appends nothing.
- `_on_pane_close_clicked` skips `close()` on a cancelled confirm, proceeds
  on confirm.
- `close()` / `close_all_teammates()` never call `confirm_manual_pane_close`
  — the automation-bypass guarantee, verified directly.

Also re-ran `test_orchestrator_done_gate.py`, `test_project_nav.py`,
`test_pane_display_state.py` for regressions on touched neighbors — all
green. `ruff check` clean on all three changed/added files.
