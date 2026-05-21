# Codex CLI Early-Crash — Hypothesis Ranking and Experiments

## Context summary
Codex CLI (v0.132.0) on Windows 11 experiences a silent crash within ~50s during the first spawn in a teammate pane. The transcript shows the initial banner and stops at `Booting MCP server: codex_apps`. Interestingly, the orchestrator's auto-respawn (second attempt) consistently succeeds. This bug surfaced following the introduction of the environment allowlist (`ab1ff5f`).

## Research findings
### codex_apps MCP server
The `codex_apps` MCP server appears to be a built-in or default plugin for the `@openai/codex` CLI. It boots immediately after the splash banner. MCP servers typically communicate via stdio, meaning they are sensitive to the state of the stdin/stdout pipes during initialization.

### codex CLI Windows compat
Node.js-based TUIs on Windows (using `pywinpty` or `ConPTY`) can be sensitive to environment variables like `COMSPEC`, `TEMP`, and `SYSTEMROOT`. While most are allowlisted, `COMSPEC` is currently missing from `_PANE_ENV_ALLOWLIST`.

### PTY + Node.js apps
Writing large payloads (bracketed-paste) to a PTY while a Node.js process is performing heavy async initialization (like booting MCP servers) can occasionally lead to buffer issues or parser confusion if the TUI library (e.g., `ink`) isn't fully ready.

## Hypothesis ranking
| # | Hypothesis | Likelihood | Impact | Falsify-ease | Score |
|---|---|---|---|---|---|
| a | **MCP boot race with paste** | 5 | 5 | 4 | 14 |
| b | **Env allowlist missing vars (COMSPEC)** | 4 | 5 | 5 | 14 |
| d | **Bracketed-paste confusion** | 3 | 4 | 4 | 11 |
| c | **PTY size mismatch (110x36)** | 1 | 2 | 5 | 8 |

*Score = Likelihood + Impact + Falsify-ease. High score indicates a priority for testing.*

## Top 3 experiments
### Experiment 1: Delay task delivery (Target: Hypo a)
**Steps:** In `src/agent_takkub/orchestrator.py`, modify `_send_when_ready` to wait an additional 5 seconds after `is_at_ready_prompt()` returns `True` specifically for the `CODEX` provider.
**Expected if true:** The crash disappears because the task payload lands after `codex_apps` has finished booting.
**Expected if false:** Crash persists at the same ~50s mark.
**Time:** ~5min

### Experiment 2: Environment bypass (Target: Hypo b)
**Steps:** In `src/agent_takkub/orchestrator.py`, temporarily change the Codex spawn path to use `env = os.environ.copy()` instead of `_build_pane_env()`.
**Expected if true:** The crash disappears. We then re-enable the filter and add missing vars (like `COMSPEC`) one by one.
**Expected if false:** Crash persists, confirming the restricted env is not the root cause.
**Time:** ~5min

### Experiment 3: Disable bracketed-paste (Target: Hypo d)
**Steps:** In `src/agent_takkub/orchestrator.py`, set `BRACKETED_PASTE_THRESHOLD = 99999` for the Codex provider to force raw text input.
**Expected if true:** The crash disappears (though input might be slow/choppy).
**Expected if false:** Crash persists.
**Time:** ~3min

## Recommendation
Run **Experiment 2 (Env bypass)** first. It is the easiest to verify and directly addresses the most recent major change (`ab1ff5f`). If that doesn't fix it, proceed to **Experiment 1 (Delay)** to address the race condition.
