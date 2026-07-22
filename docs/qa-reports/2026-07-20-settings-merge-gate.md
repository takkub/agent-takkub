# QA Report: Settings Window Merge Gate
**Date:** 2026-07-20

## 1. Test Suite
- **Command:** `python -m pytest tests/ -q`
- **Result:** ❌ FAILED
- **Details:** 7 tests failed in `test_installed_cli_bin_integration.py` and `test_installed_mode_gate.py`.
- No silent PyQt aborts were caught, but the standard test suite failed on `assert False is True` and `exists()` checks.

## 2. UI Smoke & Screenshots
- **Status:** ✅ PASSED
- **Screenshots Saved:** `docs/qa-reports/settings-merge/`
- **Views captured:**
  - 00_pipeline_builder.png
  - 01_templates.png
  - 02_roles.png
  - 03_role_overlap.png
  - 04_providers.png
  - 05_mcp_servers.png
  - 06_mcp_matrix.png
  - 07_plugins.png
  - 08_plugins_matrix.png
  - 09_skills.png
  - 10_skill_matrix.png
  - 11_users.png
- **Verification:**
  - Sidebar section headers are present and correctly mapped (PIPELINE, ROLE, PROVIDERS, TOOLS, SKILL, ACCOUNT).
  - The "Open legacy settings" button is **NOT** present.
  - Window title is confirmed as `"Takkub Cockpit — Settings"`.

## 3. Regression Checks
- **Providers page:** Structure is intact and accessible.
- **Roles page:** Replaced legacy Providers & Roles. `+New Role`, CLI selection, and toggle are present.
- **MCP/Plugins:** Page and Policy Matrix remain functional.
- **Skills page + Skill Matrix:** Present and functional.
- **Pipeline Builder / Templates / Role Overlap / Users:** Present and functional.

## 4. No Secondary Windows
- Checked for any leakage of `SettingsManagementWindow`. No buttons left that spawn the separate management window. The legacy escape hatch has been successfully removed from `SettingsWindow`.

## 5. `confirm_navigate_away` Guard
- The logic is properly implemented in the `_goto_view` and `reject` events, preventing silent data loss upon navigation/closing with unsaved changes.

## 6. Console Exceptions
- No blocking console exceptions were encountered during the programmatic UI smoke tests.

## Conclusion
❌ **BLOCKED** due to Pytest suite failures in `installed_mode_gate`.
All UI/Frontend structural changes appear to be correctly merged into the new `SettingsWindow`, but the regression in the CLI integration tests must be addressed.
