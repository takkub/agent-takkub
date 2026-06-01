# Lifecycle Diff Adversarial Review - Codex

Scope: `git diff src/agent_takkub/orchestrator.py src/agent_takkub/pty_session.py`

## Findings

### High - Fresh auto-respawn can skip task replay when cwd/message contains `(resumed)`

- Location: `src/agent_takkub/orchestrator.py:1663`, `src/agent_takkub/orchestrator.py:3366`, `src/agent_takkub/orchestrator.py:1503`
- The replay gate infers resume state with `"(resumed)" in msg`. `spawn()` returns a human string that includes the cwd (`f"{role_name} spawned in {spawn_cwd}{suffix}"`), so a project path such as `C:\work\(resumed)-migration` or any future status text containing that substring makes a fresh spawn look resumed.
- Failure mode: a pane crashes, `spawn()` starts a fresh session, but `_auto_respawn()` / `_auto_recover_stuck()` suppresses `_send_when_ready()`. The recovered pane has no conversation history and receives no task, so auto-chain can hang without obvious error.
- Why this may be missed: tests mock `spawn()` with clean strings like `"mobile spawned in /proj"` and `"mobile spawned (resumed)"`, but they do not cover cwd/status text collisions.
- Suggested direction: return structured resume state from `spawn()` or have `spawn()` set a separate last-spawn metadata flag. Do not parse a user-facing message.

### Medium - `_do_respawn()` restores popped lifecycle state before knowing respawn succeeded

- Location: `src/agent_takkub/orchestrator.py:3348`, `src/agent_takkub/orchestrator.py:3354`, `src/agent_takkub/orchestrator.py:3362`
- `_auto_recover_stuck()` calls `close()`, which intentionally pops `_session_uuids`, `_last_assigned_task`, `_auto_chain_panes`, and `_requires_commit_on_done`. `_do_respawn()` restores those entries before calling `spawn()`. If `spawn()` returns `False` after that point, the pane remains closed/empty but stale task, auto-chain, commit gate, and uuid state are left live.
- Concrete break paths include cwd validation failure (`src/agent_takkub/orchestrator.py:990`), pane registry desync (`src/agent_takkub/orchestrator.py:957`), or `PtySession.spawn()` raising (`src/agent_takkub/orchestrator.py:1456`).
- Failure mode: later manual spawn or done handling can inherit stale recovered-state assumptions from a pane that never actually respawned. Auto-chain bookkeeping can also think a dead pane still owns a pending hop.
- Suggested direction: restore only the minimum needed for `can_resume`, then roll back restored fields on failed `spawn()`, or refactor `spawn()` to accept explicit resume/task metadata without mutating the global maps before success.

### Medium - Spinner filter is too literal for Claude UI changes

- Location: `src/agent_takkub/orchestrator.py:3283`, `src/agent_takkub/orchestrator.py:3284`
- The content-delta watchdog excludes only lines containing `"esc to interrupt"`. If Claude changes the spinner copy (`"Esc to stop"`, `"Ctrl-C to interrupt"`, localized text, no text with only elapsed timer, etc.), the spinner line remains in the hash. Because the spinner typically changes glyph/time each tick, `_last_content_change_ts` keeps resetting and the stuck detector regresses to "never fires while spinner bytes arrive."
- Failure mode: the exact class of MCP/tool-call hangs this fix targets can still evade recovery after a CLI text update.
- Why this may be missed: the test uses the current exact `"esc to interrupt"` phrase and pre-seeds the filtered empty-tuple hash, so it proves the happy path but not resilience to UI text drift.
- Suggested direction: normalize/remap known spinner/status regions more broadly, for example strip volatile elapsed-time/status rows by prompt structure, cursor/status row position, or a configurable set of interrupt phrases.

### Low - Content hashing runs every watchdog tick across all working panes

- Location: `src/agent_takkub/orchestrator.py:3282`, `src/agent_takkub/pty_session.py:368`
- `display_lines()` returns `list(self.screen.display)`, i.e. the visible terminal rows, not the full scrollback. With the current spawn size this is around 36 lines, so the cost is bounded. Still, this now takes the pyte screen lock and hashes every visible row for every working pane on every watchdog tick.
- Risk: probably acceptable at current pane counts, but it is now on the UI thread and can add jitter if panes/rows grow or if `IDLE_WATCHDOG_INTERVAL_MS` is reduced. This is not a correctness blocker.

### Low - `terminate()` can wait on the current Qt thread if called re-entrantly

- Location: `src/agent_takkub/pty_session.py:348`, `src/agent_takkub/pty_session.py:351`
- `terminate()` unconditionally calls `quit()` and `wait(500)` on writer/reader threads. In normal orchestrator close paths this runs on the main thread and is bounded. If a future signal path or test calls `terminate()` from the reader/writer thread itself, waiting on the current thread is at best a warning and at worst a deadlock-like stall until timeout.
- Suggested direction: guard with `if QThread.currentThread() is not self._reader` / writer equivalent before waiting, or document that `terminate()` is main-thread-only.

## Notes On Prompted Checks

- `display_lines()` is non-`None` by implementation and returns only visible rows (`src/agent_takkub/pty_session.py:368`), so the empty/`None` concern is mostly covered by the broad `except` in the watchdog. Empty display hashes to `hash(())` and can still trigger recovery based on the seeded timestamp.
- `_from_auto_respawn` is correctly passed by the two explicit auto paths in this diff (`src/agent_takkub/orchestrator.py:1657`, `src/agent_takkub/orchestrator.py:3362`). Manual/UI/assign spawns resetting the counter appears intentional, though the broader API remains easy to misuse because the flag is private-by-convention only.
- The project namespace key is preserved in stuck recovery because `_auto_recover_stuck()` receives `project_name` from `_panes_by_project.items()` and uses `f"{project}::{role}"`. No namespace drift found in that path.
