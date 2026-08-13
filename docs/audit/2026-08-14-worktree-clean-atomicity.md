# `takkub worktree clean --force` non-atomicity — issue #187

## Root cause (proven from code, `worktree_manager.py::clean_isolated`)

Pre-fix loop body, per candidate `wt/*` worktree eligible for removal:

```python
rm = self._run([*args, row["path"]], None)          # git worktree remove
self._run(["-C", git_root, "worktree", "prune"], None)
self._run(["-C", git_root, "branch", "-D", row["branch"]], None)   # ALWAYS runs
out.append(f"{'REMOVED' if rm.ok else 'FAILED '} {row['branch']}" ...)
```

`branch -D` ran unconditionally after the remove call, regardless of `rm.ok`.
`safe_remove()` and `merge_isolated()` (the other two destroy paths in the
same file) both already gate the branch delete on the remove succeeding —
`clean_isolated()` was the one path that didn't, and it's also the only one
`--force` reaches. Confirmed with a red test on the pre-fix code
(`test_remove_failure_does_not_delete_branch` — failed with
`assert not True` on `branch -D` before the fix, passes after) before
touching the implementation.

This is exactly the 2026-08-13 21:24–21:28 incident: `git worktree remove`
failed on a Windows file lock (pane still had the directory open) →
`clean_isolated` reported `FAILED` for that row but had already deleted the
branch two lines earlier. The pane's session dir survived by luck (nothing
had been committed yet); a later timing would have made an in-progress
commit unreachable.

## Fix — new ordering, still one method, two independent guards

`clean_isolated(git_root, force=False, live_paths=frozenset())`:

1. **Live-pane guard (new, unconditional, no bypass).** Every candidate row's
   resolved path is checked against `live_paths` *before* the dirty/ahead/
   force branch. A match is always `KEEP`, force or not. No `--include-live`
   flag was added — see "Decision: no bypass flag" below.
2. **Dirty/ahead vs. `force`** — unchanged from before.
3. **Atomicity.** `branch -D` now runs only inside the `if rm.ok` branch. A
   failed `git worktree remove` now leaves both the worktree directory and
   the branch untouched, reported as `FAILED  <branch> — <reason> (ไม่ได้ลบ
   อะไร — worktree และ branch ยังอยู่ครบ)` instead of the previous bare
   `FAILED <branch>` (which gave no indication that the branch itself had
   still been deleted underneath the failure).

`safe_remove()` / `merge_isolated()` were re-read and confirmed correct
already (branch delete is behind an early-return on remove failure in both)
— out of scope, no changes made there.

## Live-pane detection — signal used and why

**Signal: `PaneState.worktree["path"]` of any pane whose `session.is_alive`
is true**, scoped to the calling Lead's project namespace.

Why this instead of cwd matching: every isolated pane already carries its
own `WorktreeInfo.as_dict()` in `PaneState.worktree` (set at assign time,
survives until the atomic pop in `close()`/`done()` — see
`worktree_manager.py`'s module docstring). That's a direct, exact record of
"this pane owns this checkout," independent of provider (claude / codex /
gemini-agy / opencode / kimi / cursor all populate it the same way — no
provider-specific branch needed, #103) and independent of whatever the
pane's PTY cwd currently reports.

New pieces wired to reach it from the CLI:

- `Orchestrator.live_worktree_paths(project)` (orchestrator.py) — walks
  `_project_panes(project)`, keeps panes with `session is not None and
  session.is_alive`, reads the matching `_pane_state[f"{project}::{role}"]
  .worktree["path"]`, returns the resolved absolute path set.
- `cli_server.py` cmd `"worktree-live-paths"` — read-only, same trust level
  as `"list"` (no Lead-only gate needed), calls the method above and replies
  `{"paths": [...]}`.
- `cli.py::_live_worktree_paths_best_effort()` — called only from the
  `clean` subcommand. Wraps `_request({"cmd": "worktree-live-paths"})` and
  swallows `RuntimeError` (no port file) / `OSError` (refused/timeout) /
  `ValueError` (bad JSON) into an **empty set**, not an error.

### Why best-effort, and why that's still safe

`cmd_worktree` is deliberately socket-free by design (its own docstring:
"works after a cockpit crash or with the cockpit closed — exactly when
cleanup is most needed"). Making `clean` hard-require the orchestrator
socket would break that invariant. But a pane can only be "live" while the
orchestrator process that spawned it is running — if the socket is
unreachable, no live pane exists to protect in the first place, so falling
back to an empty `live_paths` set is not a weaker guarantee than before,
it's simply the guard correctly having nothing to report. The guard's
actual job is protecting against exactly the observed incident: Lead pane
running `clean --force` while the SAME cockpit process has just spawned
another pane into one of the candidate worktrees. That is precisely the
case where the socket is reachable and the query succeeds.

## Decision: no `--force`-bypass flag for the live-pane guard

Task spec allowed either a separate opt-in flag (e.g. `--include-live`) or
no bypass at all, provided the reasoning is written down. Went with **no
bypass**:

- There is no legitimate operator intent for "delete the folder a
  currently-running agent is sitting in." If the pane should go, the correct
  and already-existing sequence is `takkub close --role <r>` (which pops
  `PaneState` atomically) followed by `clean`.
- Even if forced through, ripping the directory out from under a live
  process doesn't reliably work anyway — that's the literal Windows file-
  lock failure mode `--force` was trying to route around in the incident,
  so a bypass flag would mostly just turn a safe `KEEP` into a `FAILED`
  with extra steps, or worse, actually succeed and corrupt the pane's cwd
  mid-task.
- Keeps the flag surface minimal — one clear rule beats a second flag whose
  only real use case is reproducing the bug this fix closes.

## Cross-platform / multi-provider notes

- `Path(...).resolve()` is used on both sides of the live-path comparison
  (never raw string equality) so Windows drive-letter casing / trailing
  separators can't cause a false negative; no OS-specific branching needed
  since `pathlib` handles both POSIX and Windows the same way here.
- `live_worktree_paths` reads `pane.session.is_alive` and
  `PaneState.worktree`, both provider-agnostic fields already used
  elsewhere in `orchestrator.py` (e.g. `list_status_detailed`) — no
  claude-only assumption introduced.

## Tests added

`tests/test_worktree_manager.py::TestCleanIsolated`:
- `test_remove_failure_does_not_delete_branch` — the atomicity regression
  test; proven red against pre-fix code (`git stash` round-trip on just
  `worktree_manager.py`), green after.
- `test_live_pane_worktree_is_skipped_even_with_force` — live worktree KEPT
  with a `"live pane"` reason even under `force=True`; sibling worktree
  unaffected; asserts `worktree remove` was never even invoked for the live
  path.
- `test_merged_no_live_pane_still_removable` — regression guard: default
  (no `live_paths` passed) cleanup of a clean, merged, non-live worktree
  still removes it — the fix doesn't regress the common case.

`tests/test_worktree_assign.py::TestLiveWorktreePaths` (new class) — direct
`Orchestrator.live_worktree_paths` coverage: alive+worktree → reported,
dead-session+worktree → excluded, alive+no-worktree (shared cwd) →
excluded, project-scoping → cross-project pane never leaks in.

`tests/test_cli.py::TestWorktreeCli` — two new cases:
`test_clean_forwards_live_paths_from_orchestrator` (cockpit reachable →
paths flow through to `clean_isolated`) and
`test_clean_no_live_paths_when_cockpit_unreachable` (cockpit-not-running
still works, with an empty guard set). The class's shared fixture now stubs
`cli._request` to raise "no cockpit" by default so the pre-existing
clean/merge/list tests stay hermetic regardless of whether a real cockpit
happens to be running on the dev machine.

All four touched/added test files run green together; ruff check + format
clean on every touched file. Full suite not run (targeted-tests policy —
qa runs the batch gate).

## Known gap / not covered

- The live-pane guard only protects worktrees the **same cockpit process**
  currently has a live pane in. It does not (and structurally cannot,
  without a cross-process lock file) protect against a second, independent
  cockpit instance on the same machine holding a pane in one of these
  worktrees — out of scope for #187, which is specifically about the
  single-cockpit incident.
- No new CLI flag/tests were added for a live-pane bypass since the fix
  intentionally has none (see decision above) — flagging here in case a
  future report argues for one, so it's clear the omission was deliberate,
  not missed.
