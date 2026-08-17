# OpenCode Lead Remote Mirroring Implementation Report

## Summary
Resolved the issue where Remote Web / Mobile PWA did not mirror live turns or load history when `opencode` was set as the project's Lead provider (e.g. BlueParking).

## Background & Root Cause
1. In `src/agent_takkub/remote/api.py`, `lead_say` delivers prompts directly into the PTY of the Lead pane. The OpenCode CLI executed the prompt and printed its response to stdout/PTY on desktop.
2. However, Remote Control reads responses back from transcript/session stores via `LeadNotifier` (`src/agent_takkub/remote/notify.py`).
3. `opencode_spec` had `supports_remote_history = False`, and `_HISTORY_SCANNERS` only contained entries for `claude`, `gemini`, and `codex`.
4. As a result, no scanner was registered, `_tails` skipped OpenCode, and `GET /api/lead/history` returned empty messages with a degradation note.

## Solution Implemented
1. **Discovered OpenCode's Storage Architecture**:
   - OpenCode stores full session histories and tool activities in a local SQLite database (`opencode.db`) located under `~/.local/share/opencode/opencode.db` (macOS/Linux) or `%LOCALAPPDATA%\opencode\opencode.db` (Windows).
   - Tables utilized: `session` (keyed by directory / cwd), `message` (roles `user` / `assistant`), and `part` (`text`, `tool`, `reasoning`, `step-start`, `step-finish`).
2. **Created `src/agent_takkub/opencode_helper.py`**:
   - Implemented read-only SQLite URI queries (`file:<db>?mode=ro`) to eliminate lock contention or write blocking against active OpenCode instances.
   - `resolve_opencode_session`: Matches active project directory to OpenCode session UUIDs with timestamp filtering.
   - `read_opencode_session_messages`: Extracts clean user turns (stripping `[remote → lead]`) and assistant responses.
   - `list_recent_opencode_sessions`: Lists previous Lead sessions, skipping teammate tasks (`[ROLE:`).
   - `poll_opencode_delta`: Fetches incremental text/tool/question parts added since last poll offset.
   - `opencode_exec`: One-shot non-interactive command execution wrapper.
3. **Updated `src/agent_takkub/provider_spec.py`**:
   - Enabled `supports_remote_history = True` and `supports_resume = True` on `opencode_spec`.
   - Delegated discovery in `_discover_opencode` to `opencode_helper.find_opencode_executable`.
4. **Updated `src/agent_takkub/remote/notify.py`**:
   - Registered `"opencode"` adapter in `_HISTORY_SCANNERS`.
   - Added SQLite delta polling branch in `_poll_one` pushing live `lead`, `user`, `working`, and `blocked_on_picker` events.
   - Fixed uuid-less provider tail eviction so non-uuid providers (Gemini, Codex, OpenCode) preserve offsets during resync without being prematurely evicted.
5. **Updated Test Suites**:
   - Created `tests/test_opencode_helper.py` with 10 comprehensive unit tests.
   - Updated `tests/test_remote_api.py`, `tests/test_remote_mirror_diagnostics.py`, and `tests/test_remote_notify.py` so unsupported-provider negative tests target `kimi`.
   - All 206 tests pass across the entire affected test suite.
