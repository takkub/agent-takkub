# Review: Task-Start Watchdog and Boot Auto-Advance (#128)

## 1. Boot Auto-Advance (Whitelist per provider)
- **Feature**: Replaced the previous `_auto_trust` (which only watched for Claude's folder trust modal) with a generic `_boot_auto_advance`.
- **Implementation**: 
  - Added `boot_auto_advance_screens: tuple[str, ...]` to `ProviderSpec`.
  - Populated it for `claude_spec` (`"trust this folder"`, `"do you trust the contents of this directory"`, `"press enter to continue"`) and `gemini_spec` (`"do you trust the contents of this project"`, `"press enter to continue"`).
  - Also added `"do you trust the contents of this project"` to `gemini_spec`'s `ready_hard_blockers` to prevent false ready signals on initial boot.
  - Populated for `codex_spec` (`"do you trust the contents of this directory"`, `"press enter to continue"`).
  - Bounded automatic advancement to a maximum of 5 presses per boot.
  - Unrecognized hard blockers (that aren't standard busy markers like `"esc to interrupt"`) trigger an alert to the Lead.
- **Log Events**: Added `boot_auto_advance` and `boot_auto_advance_unknown`.

## 2. Task-Start Watchdog
- **Feature**: Observes each pane for up to 120 seconds after sending a task. Ensures the pane transitions into a "working" state rather than getting stuck.
- **Implementation**:
  - Implemented `_arm_task_start_watchdog` in `lead_inbox.py` and armed it in `_deliver` just after pasting the payload.
  - It checks if any generic busy markers (`"esc to interrupt"`, `"esc to cancel"`, etc.) or provider-specific working markers are displayed.
  - Also considers the task "started" if output is actively advancing (based on `session._last_output_ts`) while the session is NOT at the ready prompt (e.g. running a shell command).
  - If 120 seconds elapse without seeing any start signals, it logs a timeout and notifies the Lead with the last few lines of the screen to provide context.
- **Log Events**: Added `task_started` and `task_start_timeout`.

## 3. Testing
- Added targeted tests in `tests/test_boot_and_watchdog.py`:
  - `test_boot_auto_advance_whitelist_and_bound`: verifies automatic enter presses up to the bound.
  - `test_boot_auto_advance_unknown`: verifies Lead notification when an alien blocker halts the boot.
  - `test_task_start_watchdog_started`: verifies successful start detection.
  - `test_task_start_watchdog_timeout`: verifies 120s timeout and screen summary reporting to Lead.

All changes are pushed to branch `wt/backend-2-1784896170`.
