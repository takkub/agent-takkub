# #226 / #227 — worktree removal: MAX_PATH failure + partial-delete misreported as "kept"

## Root cause

`WorktreeManager.safe_remove` / `force_remove` / `merge_isolated` / `clean_isolated`
all deleted a worktree checkout by handing the path straight to
`git worktree remove [--force] <path>`. Git's own removal recursively deletes
the working tree itself before it will drop the `.git/worktrees/<id>`
administrative metadata, walking every file through a plain (non
extended-length) Windows path. Those paths are capped at **MAX_PATH (260
chars)** — a worktree holding an installed project's pnpm-nested
`node_modules` routinely exceeds that, so the walk fails partway through with
`Filename too long` (#226).

Because the failure happens **mid-walk**, some entries are already gone by
the time git reports the error. The old code treated any non-zero exit from
`git worktree remove` as "nothing changed, kept" and told the Lead so
verbatim: `merged ... แต่ลบ worktree ไม่ได้ (...) — เก็บที่ <path>`. That
message is a lie whenever the failure is a MAX_PATH mid-walk failure — real
incident: `.git` and `apps/` were already gone, only
`packages/prisma/scripts/`+root config survived, and the pane still pointed
at that path and could not continue working (#227). Had that pane held
uncommitted work, it would have been unrecoverable — `.git` itself was gone.

A second, independent gap: `merge_isolated` (unlike `clean_isolated`, fixed
for the same class of bug in #187) had **no live-pane guard at all** — it
would delete a worktree directory a currently-alive pane's cwd still pointed
at, merge or no merge, dirty-check or no dirty-check.

## Fix

`src/agent_takkub/worktree_manager.py`:

1. **Long-path-safe delete is now the only thing that ever recursively
   touches a worktree's files.** `git worktree remove` is called *only after*
   the directory is already confirmed gone from disk, and always with
   `--force` at that point (safe — it only removes the small
   `.git/worktrees/<id>` admin refs, not files, once the directory itself is
   already gone) — the git delete path that produced the partial-delete bug
   is never exercised again.

2. **`_stage_for_delete`** renames the checkout dir aside first
   (`<name>` → `.trash-<name>-<pid>`, same managed-root parent). A rename
   only rewrites one directory entry, not a recursive walk, so it is immune
   to MAX_PATH regardless of what's nested inside, and it is atomic: it
   either fully succeeds (tracked path now **completely** gone) or fully
   fails (tracked path **fully untouched**). There is no partial state ever
   observable at the path anything else — a pane's cwd, `git worktree list`,
   PaneState — still points at.

3. **`_rmtree_long_path_safe`** then deletes the staged copy through the
   Win32 extended-length (`\\?\`, `\\?\UNC\` for UNC roots) path form, which
   has no MAX_PATH limit, with an `onerror` hook that clears a lingering
   read-only bit (common on git-checked-out files) and retries once. Because
   `onerror` swallows exceptions (`shutil.rmtree` must never raise
   mid-walk), success is verified by an **explicit post-hoc existence
   check** — the function never trusts `rmtree`'s silent return alone.

4. **`remove_worktree_tree(path)`** composes the two: returns
   `(removed_from_original, message, leftover_path)`. `removed_from_original`
   is `False` **only** when the initial rename itself failed — the one case
   where "kept, fully intact" is an accurate report. It is `True` the moment
   the tracked path no longer exists, whether there was nothing to delete,
   the delete fully succeeded, or it partially failed (any survivors live
   only under the *staged* `leftover_path`, never at the tracked path). This
   directly satisfies #227's requirement #2: never say "kept" when a partial
   delete actually happened.

   Leftover staged dirs (`.trash-*`) sit in the same managed worktree root
   (`DATA_HOME/worktrees/<project>/`) that `disk_usage.py`'s existing
   `orphan-worktrees` / `orphan-worktrees-review` sweep already scans and
   classifies (#132) — no new cleanup path was needed; they're picked up and
   retryable via the existing `takkub prune` flow automatically, satisfying
   #226's suggestion #2 ("register the path so `takkub worktree clean` can
   retry it later") for free.

5. **Live-pane guard added to `merge_isolated`** (`live_paths` param,
   mirroring `clean_isolated`'s #187 fix): if the worktree path is currently
   held by a live pane, the merge still happens (it only touches `git_root`,
   not the worktree dir) but removal is skipped and reported clearly —
   `worktree ยังมี pane ใช้งานอยู่ (live) จึงไม่ลบ`. Wired through
   `cli.py::cmd_worktree`'s `merge` subcommand, which now calls
   `_live_worktree_paths_best_effort()` before merging, exactly like `clean`
   already did.

6. **Stale branches (#226 req #4)**: unchanged in spirit — a branch is only
   deleted once its worktree's on-disk removal is confirmed. If removal now
   succeeds where it used to fail (the common MAX_PATH case), the branch is
   cleaned up too instead of lingering in `git worktree list`/`git branch`.

`safe_remove`, `force_remove`, and `clean_isolated` were updated the same
way for consistency — they had the identical bug shape (delete via bare
`git worktree remove`, trust its exit code as the whole story).

## Safety property proven

For every one of `safe_remove` / `force_remove` / `merge_isolated` /
`clean_isolated`, after this fix there are exactly two reportable outcomes
for the tracked worktree path, never a third:

- **Kept, fully intact** — only when the initial rename failed. Nothing was
  touched. Safe to keep working there.
- **Gone from the tracked path** — rename succeeded. Any residue lives only
  in a clearly-marked, unregistered `.trash-*` sibling that nothing
  (pane cwd, git, PaneState) is pointing at.

There is no code path left that can leave the tracked path half-deleted and
still call it "kept".

## Test evidence

`tests/test_worktree_manager.py`:

- `TestRemoveWorktreeTree` — `remove_worktree_tree` unit coverage on a real
  (small) filesystem tree: missing path is trivial, full delete leaves the
  original path gone, a monkeypatched rename failure leaves the original
  **fully intact** (`removed=False`), and a monkeypatched partial-delete
  failure (`_rmtree_long_path_safe` forced to fail) still reports
  `removed=True` with the original path **actually gone** and the leftover
  surfaced — i.e. the #227 misreport is structurally impossible now.
- `TestLongPathDeleteWindows` (`skipif` non-Windows, genuinely **runs** on
  this machine — not skipped) — builds a real nested directory tree past 260
  chars (`len(str(deep)) > 260` asserted) mirroring the pnpm-nested
  `node_modules` shape from #226's repro, and proves
  `_rmtree_long_path_safe` / `remove_worktree_tree` delete it completely.
  This is the actual MAX_PATH failure mode reproduced and fixed, not a mock.
- `TestMergeIsolated::test_live_pane_worktree_merged_but_not_removed` — new
  coverage for the #227 live-pane guard: merge still runs, removal doesn't.
- All pre-existing `TestSafeRemove` / `TestMergeIsolated` / `TestCleanIsolated`
  tests pass unmodified — the redesign is behavior-compatible for every
  scenario they already covered (FakeRunner tests operate on non-existent
  paths, which `remove_worktree_tree` correctly treats as "nothing to
  delete, proceed").

`tests/test_cli.py::TestWorktreeCli`:

- `test_merge_forwards_live_paths_from_orchestrator` /
  `test_merge_no_live_paths_when_cockpit_unreachable` — new coverage proving
  `cmd_worktree`'s `merge` subcommand now queries and forwards live-pane
  paths exactly like `clean` already did, and degrades to an empty set
  (not a crash) when the cockpit isn't reachable.

Full run: `tests/test_cli.py::TestWorktreeCli` (19 tests) +
`tests/test_worktree_manager.py` (78 tests) — **97 passed, 0 failed, 0
warnings**. `ruff check` + `ruff format --check` clean on all 4 changed
files.

## Files changed

- `src/agent_takkub/worktree_manager.py` — long-path-safe delete helpers
  (`_win_long_path`, `_path_exists_long_safe`, `_clear_readonly_and_retry`,
  `_rmtree_long_path_safe`, `_stage_for_delete`, `remove_worktree_tree`);
  `safe_remove` / `force_remove` / `merge_isolated` / `clean_isolated`
  rewired to use them; `merge_isolated` gained the `live_paths` param.
- `src/agent_takkub/cli.py` — `cmd_worktree`'s `merge` branch now queries
  `_live_worktree_paths_best_effort()` and forwards it.
- `tests/test_worktree_manager.py`, `tests/test_cli.py` — new coverage
  listed above; existing `_FakeWtMgr.merge_isolated` signature extended with
  `live_paths` to match the real method.

## Scope notes / not done

- Did not touch `WorktreeManager.safe_remove` / `force_remove` call sites —
  grepped and found none currently wired into `orchestrator.py` (only
  `clean_isolated`/`merge_isolated` are reachable via the CLI today); fixed
  them anyway for consistency since they share the exact same bug shape and
  are public API on the class.
- Did not add a dedicated "list/retry `.trash-*` leftovers" command — the
  existing `disk_usage.py` orphan-worktree sweep already generically covers
  any unregistered directory under the managed worktree root, which is
  exactly where a leftover `.trash-*` lands.
