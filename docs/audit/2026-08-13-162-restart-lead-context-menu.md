# #162 — "🔄 Restart Lead" on project tab right-click menu

## What changed
- `main_window.py::_on_tab_context_menu` — added a "🔄 Restart Lead" action
  between "Edit project rules…" and "Close project".
- New `main_window.py::_on_restart_lead_from_menu(proj_name, index)`:
  - Confirms via `QMessageBox.question` (default button = Cancel), warning
    about in-flight panes for that project (same wording pattern as
    `update_panel.py::_on_restart_cockpit_clicked`).
  - If the right-clicked tab isn't the currently active one, switches to it
    first (`self.tabs.setCurrentIndex(index)`), which fires the existing
    `_on_tab_switched` → `set_active_project(...)` path.
  - Delegates to the existing `_restart_lead_for_active_project()` — no
    changes to that method or to `_respawn_lead_post_restart` were needed,
    since both already resolve to "whichever project is active" via
    `orchestrator._resolve_project(None)`.

## Why this shape (diff-size reasoning)
`_restart_lead_for_active_project` and the orchestrator calls inside it
(`close`, `close_all_teammates`, `spawn`) all default `project=None` →
"currently active project". Generalizing them to take an explicit project
param would have touched 3 methods + their tests for no behavioral gain.
Activating the target tab first reuses that existing "active project"
resolution untouched — user sees the tab switch (so it's obvious which
project's Lead is restarting) and the diff stays to one menu entry + one
new handler.

## Confirm-dialog parity
Mirrors `_on_restart_cockpit_clicked`'s pattern: counts working/active panes
scoped to the target project via `orch._project_panes(proj_name)`, warns if
>0, defaults the dialog to Cancel.

## Tests
`tests/test_restart_lead_context_menu.py` (4 cases, all passing):
- confirm+inactive tab → switches tab, then restarts
- confirm+already-active tab → skips the switch, still restarts
- cancel → neither switch nor restart happen
- working-pane count in the confirm body is scoped to the target project
  only (not every open project)

Also re-ran `test_restart_cockpit.py`, `test_user_actions_provider_switch.py`,
`test_lead_self_protection.py` — all green, no regressions.

## Note
Run via the shared venv (`C:\Users\monch\WebstormProjects\agent-takkub\.venv`)
with `PYTHONPATH` pointed at this worktree's `src/` — the shared venv's
editable install resolves to the main repo's `src/`, not the worktree's, so
plain `python -m pytest` from inside a worktree silently tests the wrong
source tree.
