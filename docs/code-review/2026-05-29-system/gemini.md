# System Review — agent-takkub cockpit (2026-05-29)
**Reviewer:** Gemini 2.0 Flash (via takkub gemini)

## 1. Correctness — Bug, Edge Case, Race Condition
- **Fragile TUI State Detection**: `is_at_ready_prompt` and `is_at_trust_prompt` in `pty_session.py` rely on hard-coded text markers (e.g., "bypass permissions", "shift+tab to cycle"). This makes the orchestrator highly sensitive to Anthropic's CLI output changes. A minor update to Claude Code could break the idle watchdog or auto-trust features.
- **Synchronous CLI calls in UI thread**: `CliServer` dispatches commands that call into `Orchestrator`. Some operations, like `takkub issue` (via `issues.py`) and `shared_dev_tools.py` checks, execute `subprocess.run` or `gh` commands synchronously. Since `CliServer` runs on the Qt main thread, a slow network or GitHub API response will freeze the entire cockpit UI.
- **Bracketed Paste Race**: `_PASTE_ENTER_DELAY_MS` (800ms) assumes Claude Code will finish rendering the "Pasted text" placeholder within that window. On heavily loaded systems or with extremely large pastes, if the render takes >800ms, the subsequent `\r` may be consumed as a newline inside the paste buffer instead of submitting the task.
- **PTY Read Latency**: The `_ReaderThread` in `pty_session.py` uses fixed `time.sleep` (20ms/40ms) when encountering `EOFError` or empty reads. While necessary for `pywinpty 3.x`, it introduces a synthetic floor to responsiveness that might be noticeable during high-output bursts.

## 2. Architecture — Module Boundary, Coupling
- **Orchestrator Bloat**: `orchestrator.py` (~3000 lines) has become a "God Object". It manages pane lifecycles, watchdog timers, vault/hot.md snapshotting, harvest logic, and even re-exports constants from other modules. This makes it difficult to unit test in isolation and increases the risk of side effects.
- **Tight Qt Coupling**: The core logic is deeply intertwined with PyQt6 (signals/slots, QTimer, QTcpServer). While effective for a desktop app, it makes it impossible to run the orchestrator/routing logic in a headless CLI-only mode without mocking a substantial part of the Qt ecosystem.
- **Routing Planner Logic**: The regex-based approach in `routing_planner.py` is clean and testable, but the `_derive_primary_role` fallback to `backend` is a significant architectural assumption that might misroute ambiguous frontend/devops tasks.

## 3. Security — Pane Env, Injection, Auth
- **Robust Multi-Layer Auth**: The 3-layer security model in `cli_server.py` (Role gate + Token check + Spoof guard) is excellently implemented. Using `secrets.compare_digest` for token validation correctly prevents timing attacks.
- **Effective Env Sandboxing**: The `_PANE_ENV_ALLOWLIST` in `pane_env.py` is a critical defense-in-depth measure. It successfully prevents accidental leakage of `ANTHROPIC_API_KEY`, GitHub tokens, and other developer secrets to teammate panes that only need an OAuth-authenticated Claude session.
- **MCP Credential Protection**: `shared_dev_tools.py` correctly identifies and skips "secret-bearing" MCP entries (Authorization headers, etc.) when merging user configs. The opt-in `TAKKUB_INCLUDE_PMS` flag follows the principle of least privilege.
- **Path Traversal Guard**: `validate_name` in `config.py` provides strong protection against directory traversal via role or project names by using a very restrictive regex.

## 4. Maintainability — Readability, Naming, Dead Code
- **Exceptional Documentation**: The codebase is remarkably well-documented. Comments explain the "why" (e.g., the specific reason for pinning browser MCP versions to avoid npx registry hits) which is invaluable for long-term maintenance.
- **Quirk Management**: The project does a great job of encapsulating Windows-specific "horrors" (ConPTY backend, console window hiding, `COMSPEC` injection for Node.js) into dedicated helpers like `_win_console.py` and `pty_session.py`.
- **Leftover Guard Imports**: `orchestrator.py` still imports `_LEAD_GUARD_ALLOW_TOOLS` and `_LEAD_GUARD_WRITE_TOOLS`, which appear to be remnants of a discarded "hard deny" security model. These should be pruned if the policy has moved entirely to `CLAUDE.md`.

## 5. Blind Spot — Things overlooked
- **Token Churn on Respawn**: The auto-respawn logic (`_on_session_exit` → `spawn`) is designed for resilience, but it doesn't account for the "token cost" of re-injecting large system prompts and MCP schemas into a fresh session. A "crash loop" that stays under the `AUTO_RESPAWN_MAX` cap could still be expensive in terms of context usage.
- **Task Replay Idempotency**: `_auto_respawn` automatically replays the last assigned task. If the task itself caused the crash (e.g., a specific input triggering a bug in Claude Code), the orchestrator will dutifully trigger the crash again up to the retry limit. There is no "back-off" or "safe-mode" spawn after a crash.
- **Missing Resource Cleanup**: While `AgentPane` detaches sessions, the `PtySession` objects and their associated threads rely on `terminate()` and GC. In long-running cockpit sessions with hundreds of teammate spawns, there might be latent resource exhaustion (handles, memory) if `pywinpty` or the threads don't exit cleanly.
