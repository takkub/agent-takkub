# Claude Hook Pane-State Cross-Check

Scope: reviewed `spawn_engine.py`, `cli_server.py`, `pty_session.py`, plus current lifecycle/watchdog code in `orchestrator.py` because it owns `working`/`done` state and PTY idle reminders.

## 1. Block-loop safety

Risk: `stop_hook_active` is necessary but not sufficient for done-gate safety. It only prevents Claude Code from recursively re-entering the Stop hook while a Stop hook is already active. It does not remember that this pane/session/turn has already been blocked once. If the model receives the nudge, ignores `takkub done`, and stops again, the next Stop event can block again forever.

Risk: not every pane should have a done requirement. Lead is explicitly forbidden from `done`; `shell` is user-driven; a teammate opened via manual `spawn`/empty task can be exploratory. Current watchdog only nudges non-Lead panes that are still `pane.state == "working"`, which normally comes from `assign()`/`send()`. A Stop hook that fires based only on `TAKKUB_ROLE`/`TAKKUB_PROJECT` would be broader and could block legitimate manual use.

Risk: `send --to lead` is a real blocking state today. The watchdog suppresses forgot-done reminders for 30 minutes when `blocked_on_lead_ts` is set. The Stop hook should honor the same suppression or it will block the teammate right after it asks Lead a clarification.

Recommendation:

- Gate done-block on server-side pane state, not just env: only `non-lead`, live pane, `pane.state == "working"`, has an assigned-task marker such as `PaneState.last_assigned_task` or pipeline/shard/auto-chain metadata, and not blocked on Lead/rate-limited/TTY prompt.
- Make Stop blocking one-shot per task epoch: store `done_gate_blocked_epoch` in `PaneState` keyed by `(project, role, session_uuid, assignment_epoch)` or equivalent. First missing-done Stop returns `decision:block`; later missing-done Stops in the same epoch return allow and let the idle watchdog/harvest path handle it.
- Clear the one-shot flag on new `assign()`, `send()` that starts real work, respawn with a new assignment, and `done()`/`close()`.
- Keep the existing PTY idle reminder as the repeated nudge mechanism. The Stop hook should be a single immediate nudge, not a loop.

Severity: blocker if backend currently plans "block once" with only `stop_hook_active`; that still permits one block per Stop event.

## 2. Double-signal race

Current model: `assign()` queues work and stores `last_assigned_task`; `_send_when_ready()` writes the prompt; `done()` atomically pops `PaneState`, clears `_idle_state`, marks pane `done`, then closes after 2.5s. PTY scraping is poll-based and can lag or see stale footer text.

Race scenarios:

- Stop hook reports turn-end while the PTY still shows a busy footer or before the screen repaint reaches ready. If the orchestrator treats hook turn-end as idle immediately, it may nudge before the model output visibly settles.
- PTY watchdog sees ready just before a hook Notification/Stop event marks the pane busy/turn-active for the next user message. A stale `_idle_state.first_idle_ts` can survive and fire too early after the next turn.
- `done` and Stop arrive close together. `done()` pops `PaneState`; a late Stop hook must not recreate state or re-nudge a pane already in `done`/closing.
- Hook and scraping both emit idle for the same turn; without idempotency the game/status view may flip duplicate idle events, and the reminder cooldown may be computed from two clocks.

Recommendation:

- Add a monotonic per-pane `state_epoch` or `turn_epoch`. Increment on `assign()/send()` delivery and on `done()/close()/spawn()`. Every hook event carries or is stamped with the current server epoch when received. Ignore events for panes whose token/session no longer matches.
- Source precedence: `done/close/spawn` lifecycle events are terminal and win over hooks and scraping. Hook `Stop`/`Notification(idle_prompt)` wins over PTY scraping for idle/turn-end only while the pane is still live and `working`. PTY scraping remains fallback when no fresh hook signal exists.
- Freshness rule: keep `last_hook_ts` and `last_pty_ts`. If a hook idle/turn-end was received within a small freshness window, PTY cannot overwrite it to busy unless PTY sees a hard blocker (`esc to interrupt`, trust prompt, TTY prompt) after the hook timestamp. If PTY idle arrives first, a later hook busy/turn-start clears `_idle_state.first_idle_ts`.
- Idempotency rule: normalize both sources into one reducer, e.g. `observe_pane_signal(project, role, source, kind, ts, session_uuid, turn_epoch)`. The reducer drops duplicate `(session_uuid, turn_epoch, kind)` and never performs side effects directly except through state transitions. Reminder injection is driven from reduced state, not from raw events.
- Done idempotency: because `done()` pops pane state and revokes pane token, hook processing after done should be a no-op unless the same role has a new live session/epoch.

## 3. Windows hook quoting

Risk: Windows spawn path converts argv to one command line with `subprocess.list2cmdline()` before pywinpty. Existing code already documents quote leakage for values containing spaces. Inline `--settings` JSON with embedded hook command JSON magnifies this: JSON quotes, backslashes, `%VAR%` expansion under `cmd`, PowerShell quoting rules, and paths like `C:\Program Files\...` are all fragile.

Risk: Claude Code hook `command` is usually executed by the platform shell, not by takkub's Python argv list. On Windows that may mean `cmd.exe /c` or PowerShell depending on Claude implementation/version. A command that is safe in POSIX `sh` can break under `cmd`, and vice versa. Relying on PATH can also fail because GUI-launched cockpit has a thinner PATH; spawn currently prepends `REPO_ROOT/bin`, but hook subprocess resolution depends on Claude's child environment and shell semantics.

Recommendation:

- Avoid complex inline shell commands in hook settings. Put the hook command in the smallest possible, shell-neutral form and move complexity into `takkub _hook`.
- Best cross-OS command if Claude supports argv-style hook config: command/executable plus args array, e.g. executable `takkub`, args `["_hook"]`. This avoids shell quoting entirely.
- If Claude only accepts a string command, use a bare PATH-resolved command with no quoting-sensitive args: `takkub _hook`. Ensure `spawn_engine.py` prepends `REPO_ROOT/bin` before spawning Claude, as it already does, and ensure the Windows shim is `takkub.cmd`/`takkub.exe` discoverable from that bin dir.
- If PATH resolution is not reliable on Windows, prefer a generated per-pane hook wrapper script with no spaces in its path under runtime, such as `runtime/hooks/<project>/<role>/hook.cmd`, and set command to that path. The wrapper can call the exact Python/takkub entrypoint with properly quoted paths. On POSIX use sibling `hook.sh`.
- Avoid embedding raw JSON as a shell argument to `takkub _hook`; the hook already receives stdin JSON. Context should come from env (`TAKKUB_ROLE`, `TAKKUB_PROJECT`, preferably `TAKKUB_PANE_TOKEN` too) plus stdin.
- Prefer passing `--settings` value via a temp file if Claude supports it, or minified JSON as a single argv element only if verified on Windows. Do not build a shell string for `claude --settings ...`; keep it in the existing `argv` list.

Security/correctness note: `_hook` should authenticate like `done`/`send`. Env role/project is forgeable by any process in the pane; include `TAKKUB_PANE_TOKEN` in hook requests and have `cli_server` derive `(project, role)` from the token, matching the existing token gate.
