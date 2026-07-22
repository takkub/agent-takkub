# QA Report: Provider Model Selection Batch Gate
Date: 2026-07-20

## 1. UI Smoke Test (Cockpit)
- **Status:** PASS
- **Details:** 
  - Navigated to `Providers & Roles` → Selected `gemini`.
  - The `Model` input was correctly enabled.
  - Entered `gemini-3.1-pro` and clicked Save & Apply.
  - Verified `~/.takkub/provider-models.json` successfully saved `{"gemini": "gemini-3.1-pro"}`.
  - Reloaded the page and confirmed the value persisted in the UI.
  - Cleared the input field and saved; verified the key was successfully removed from the JSON.
  - Saved screenshot of the `Model` channel in `docs/qa-reports/ui-smoke-model.png`.

## 2. CLI Round-Trip
- **Status:** PASS
- **Details:**
  - Ran `takkub provider model kimi k2.5` successfully.
  - Ran `takkub provider model kimi` which correctly displayed `k2.5`.
  - Ran `takkub provider list` which correctly displayed the model suffix.
  - Ran `takkub provider model kimi --clear` which successfully reset it.
  - Ran `takkub provider model kimi` which correctly reverted to `(provider default)`.

## 3. Spawn ARGV Spot-Check
- **Status:** PASS
- **Details:**
  - Set `codex` model to `gpt-4o` via CLI.
  - Ran `pytest tests/test_provider_models.py -q -k spawn` which successfully passed.
  - Cleared the `codex` model.

## 4. Console Exception Check
- **Status:** PASS
- **Details:** No exceptions encountered during the UI interaction tests and CLI testing.

## 5. Full Pytest Suite
- **Status:** PASS
- **Details:** The full pytest suite (python -m pytest tests/ -q) passed successfully with 100% completion (all pass, 2 skips).
