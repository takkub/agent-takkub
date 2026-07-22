# QA Report - 2026-07-20 Final Gate

## 1. Full suite test
Command: `.venv/Scripts/python.exe -m pytest tests/ -q`
Result: PASSED. All 4060 tests passed (2 skipped, 0 failed).

## 2. Lint + format
Command: `ruff check src/ tests/` and `ruff format --check src/ tests/`
Result: PASSED. All checks passed and 338 files already formatted.

## 3. Sanity assertion
Command: `QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -c "from PyQt6.QtWidgets import QApplication; app=QApplication([]); from agent_takkub import settings_window as sw; w=sw.SettingsWindow(None, project='agent-takkub'); print(len(w._nav_buttons), w._stack.count(), len({s for _,_,s in sw._NAV_VIEWS}))"`
Result: PASSED. Output was `12 12 6` confirming we instantiated the correct class (`SettingsWindow`) with 12 navigation buttons, 12 stack views, and 6 sections.

## 4. UI verify + screenshot
Verified the UI using programmatic tests:
- Checked for legacy button "Open legacy settings": Not found in UI.
- Captured sidebar full view: `00-sidebar.png` and `00-sidebar-full.png`.
- Captured all 12 views:
  - `view-00-pipeline-builder.png`
  - `view-01-templates.png`
  - `view-02-roles.png`
  - `view-03-role-overlap.png`
  - `view-04-providers.png`
  - `view-05-mcp-servers.png`
  - `view-06-mcp-matrix.png`
  - `view-07-plugins.png`
  - `view-08-plugins-matrix.png`
  - `view-09-skills.png`
  - `view-10-skill-matrix.png`
  - `view-11-users.png`
- Providers view: Selected Gemini, set model `gemini-3.1-pro`, saved and verified `provider-models.json` successfully.
- Navigation dirty state warning (confirm_navigate_away): Verified the system correctly intercepts navigation if dirty state is present (programmatic messagebox assert passed).

## 5. Exceptions
No console exceptions encountered during testing. All commands executed cleanly without stack traces.

All validations passed. Ready for merge.
