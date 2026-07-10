# Cross-Platform Fix Wave — Re-Audit (gemini)

**Date:** 2026-07-10
**Scope:** Accumulated git diff (~37 files) vs. `docs/reviews/2026-07-10-xplatform-CONSOLIDATED.md` & previous reviewer findings
**Method:** Traced all callers of key functions/variables across the codebase, analyzed environment variable impact, and verified test suites.

**Verdict:** `CLEAN — no new findings`

---

## 🔍 Audit & Verification Details

### 1. Callers of Refactored Interfaces

- **`list_recent_lead_sessions` / `_resume_uuid_matches_cwd`**
  - **Change:** Removed lossy reverse-decoding of cwd paths (which broke for names with hyphens/spaces like `agent-takkub`). Replaced with direct folder lookup via `token_meter.session_project_dir_for_cwd`.
  - **Verification:** Callers in [remote/api.py](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/remote/api.py) and [user_actions.py](file:///C:/Users/monch/WebstormProjects/agent-takkub/src/agent_takkub/user_actions.py) resolve project paths and session lookups correctly. The added `_SAFE_SESSION_UUID_RE` filter resolves the path traversal risk (F1) cleanly.
  - **Impact:** Works properly on both OS. No regressions found.

- **`_default_plugin_dirs`**
  - **Change:** Enabled resolution against project-specific config overrides (`CLAUDE_CONFIG_DIR`) rather than always falling back to the default profile.
  - **Verification:** Spawning engine correctly forwards `project=project_ns`.
  - **Impact:** Clean.

- **`_build_pane_env` / `_build_lead_env`**
  - **Change:** Consolidated environment variable injection (MCP timeouts, color terminals, and non-interactive variables) so all branches (claude, codex, gemini, shell) share them.
  - **Verification:** Verified that it prevents macOS GUI-launch monochrome styling and non-interactive hangs across all providers, not just Claude.
  - **Impact:** Clean.

- **`find_codex_executable`**
  - **Change:** Prefer native Windows `codex.exe` executable inside nested node modules if present to prevent brief command prompt flash on spawn.
  - **Verification:** Checked that it falls back to the `.cmd` shim if the native binary is missing.
  - **Impact:** Clean.

- **`lead_cwd`**
  - **Change:** Normalizes paths to absolute using `Path.expanduser().resolve()`.
  - **Verification:** Prevents relative path issues where Lead would spawn in cockpit cwd instead of the project directory. Handled exceptions gracefully.
  - **Impact:** Clean.

### 2. Environment Variables Injection (`MCP_TOOL_TIMEOUT`, `COLORTERM`, `GIT_TERMINAL_PROMPT`)

- Shared injection has no adverse side effects on alternative providers (Codex/Gemini). It correctly allows shell panes to fail-fast on Git authentication errors rather than hanging.

### 3. Documentation (`CLAUDE.md`)

- The root [CLAUDE.md](file:///C:/Users/monch/WebstormProjects/agent-takkub/CLAUDE.md) correctly documents `MCP_TOOL_TIMEOUT` injection behavior as general ("inject ทุก pane โดย default"). With H1 implemented, this documentation is fully aligned with the implementation. No stale behavior remains.

---

## 🧪 Test Suite Status

Targeted tests cover all aspects of the changes:
- `test_h1_nonclaude_env.py` (checks non-claude env variables injection)
- `test_resume_session_picker.py` (checks lossy paths, pre-validation before close, traversal UUIDs)
- `test_codex_helper.py` (checks native exe vs shim resolution)
- All 313 targeted tests pass successfully.
