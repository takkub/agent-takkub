# #162 — `assign --isolation worktree` collides when fired 3x at the same bare role name

## Repro (reported)

`takkub assign --role backend --isolation worktree "<task>"` fired 3 times back-to-back,
all using the bare role name `backend` (no `#N` suffix). Task 1 and 2 were silently
"replaced" before they ever started, and task 3 ended up running/reporting task 1's
content instead of its own.

## Root cause

Pane identity (`_project_panes()` dict key, `PaneState` via `_exit_key(project, role)`,
the spawn-time one-shot task payload) is keyed purely by `role_name`. `--isolation
worktree` creates a **fresh git worktree on disk on every call** (`WorktreeManager.create`
takes a timestamp, so each call gets its own branch/path), but it still dispatches into
that same role-keyed pane slot via `_assign_dispatch`.

Traced end to end (`orchestrator.py::assign` → `_assign_with_worktree` →
`_assign_dispatch` → `spawn_engine.py::spawn`):

1. Call 1 (`backend`, worktree A) writes the full task onto the shared `PaneState`
   (`spawn_initial_task` / `spawn_initial_task_state`) and spawns the real pane in
   worktree A's cwd. The Claude process boots with **task 1** as its system prompt.
   `PtySession.is_alive` flips `True` almost immediately (`pty_session.py:727`), well
   before the CLI is actually at a ready prompt.
2. Call 2 (`backend`, worktree B) runs `_assign_dispatch` again. It sees `pane_is_running
   is True` (session alive), so it does **not** re-spawn — `spawn()` short-circuits with
   `"backend already running"`. But `_assign_dispatch` had *already* overwritten the
   shared `PaneState`'s `spawn_initial_task_state` to `"requested"` before calling
   `spawn()` — a state nothing ever transitions back out of once `spawn()` early-returns
   on an already-alive pane. `_assign_dispatch` then falls through to
   `_send_when_ready(role_name, paste_text, ...)`, **pasting task 2's pointer text into
   the pane that is still running task 1** — worktree B is created on disk and never
   entered by any process.
3. Call 3 does the same thing again, pasting task 3's pointer into the same still-task-1
   pane. Worktree C is likewise orphaned.

Net effect: only worktree A's process ever actually runs; it boots on task 1's content
and then receives two unrelated paste-interjections (task 2's and task 3's pointers)
into its terminal input while it's still working — exactly the "task 3 reports task 1's
content" symptom, since the one real spawn never used task 3 (or task 2) as its system
prompt at all. Worktrees B and C sit on disk, fully created (branch + files), touched by
no pane.

A second, independent bug compounds this at the transport layer: `cli_server.py`'s
`assign` dispatch runs the real `orchestrator.assign()` call **deferred** (`QTimer.
singleShot`, fire-and-forget) and acks the socket with an unconditional `ok=True`
*before* that deferred call even runs — its return value is discarded entirely. So even
a correct in-process rejection inside `assign()` would never reach the CLI caller; the
Lead would still see `ok: true`.

## Fix (scope: option 1 — hard-reject repeat bare-role worktree assigns)

Added `Orchestrator._worktree_bare_role_collision(role_name, project)`
(`orchestrator.py`): returns an error string when `isolation == "worktree"`, the role
name has no `#N` shard suffix (`_split_shard` gives a `None` index), and a pane is
already registered under that exact key in `_project_panes()` — regardless of whether
it's alive, still spawning, or otherwise in flight. Wired in two places:

1. **`Orchestrator.assign()`** — authoritative check, protects every caller that reaches
   `assign()` directly (worktree finalize, auto-respawn replay, tests), not just the
   socket path.
2. **`cli_server.py`**, synchronously, before the `QTimer.singleShot` deferred dispatch
   and its unconditional `ok=True` ack — mirrors the existing `#143` cwd-validation
   precheck pattern in the same function (`cwd_validation_error`). Called defensively via
   `getattr(self._orch, "_worktree_bare_role_collision", None)` so a minimal test double
   without the method still degrades to "no check" rather than crashing (same pattern the
   `_resolve_project` precheck already uses).

The error message tells the caller to use `<role>#N` instead of the bare name, or to
close/wait for the existing pane first.

**Deliberately out of scope (not implemented):** option 2 — re-keying the whole
pane/task-ledger identity system by pane/worktree instance rather than bare role name.
That's a much larger, cross-cutting change (`_project_panes`, `PaneState`/`_pane_state`,
IPC payloads, UI pane widgets, task ledger) for a problem the hard-reject already closes
at the one place it's reachable (`assign --isolation worktree`). A bare-name re-assign
under `isolation="shared"` — the normal "send a follow-up task to a running pane" flow —
is intentionally left untouched; the guard only fires for `isolation == "worktree"`.

## Tests added

- `tests/test_worktree_assign.py::TestBareRoleWorktreeCollision` (5 cases): 2nd/3rd bare
  `--isolation worktree` assign against an existing pane is rejected;
  `<role>#N` sidesteps the guard; no existing pane → no collision; plain
  `isolation="shared"` re-assign to a running pane is unaffected.
- `tests/test_cli_server.py::TestSyncWorktreeCollisionCheck` (4 cases): collision is
  rejected synchronously before the ack (never reaches the deferred `assign()` call);
  no-collision case still dispatches normally; `isolation="shared"` never even calls the
  check; a stub orchestrator missing the method degrades gracefully instead of crashing.

`pytest -k "assign or worktree or fanout or spawn"` (166 tests) — all pass except one
pre-existing, unrelated failure (`test_lead_context_compact.py::
TestParallelModeWorktreeRule::test_solo_mode_has_no_parallel_block`, a mojibake/encoding
assertion in Lead-context rendering, confirmed via `git stash` to fail identically on the
base commit — untouched by this change). `lint-imports` — 23/23 contracts kept.

## Files changed

- `src/agent_takkub/orchestrator.py` — `_worktree_bare_role_collision()` + guard in `assign()`
- `src/agent_takkub/cli_server.py` — synchronous precheck before the assign ack
- `tests/test_worktree_assign.py`, `tests/test_cli_server.py` — new test coverage
