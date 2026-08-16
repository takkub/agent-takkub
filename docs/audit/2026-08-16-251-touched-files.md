# Issue #251 — shared-tree `ไฟล์ที่แตะ` attribution

Date: 2026-08-16

## Evidence reviewed

`gh issue view 251 --json number,title,body,comments,url` was read before implementation.
The report contains two consecutive production cases:

- `devops` changed only root `package.json`, but its done digest reported 17
  paths. Thirteen belonged to the concurrently-running `backend` pane, and
  pre-existing untracked screenshots were included too.
- `frontend` changed only `packages/ui/tsconfig.json`, but its digest reported
  four paths, including three root screenshots that had existed untouched for
  the whole session.

The implementation for #245 stored only assign-time HEAD. At done it unioned
`HEAD` diffstat with the checkout's complete live porcelain output. Therefore
committed interval data had a baseline, while uncommitted data did not; every
pre-existing dirty path looked as if this pane touched it.

## Decision: option (ก), cheap dirty-path snapshot

At shared-tree assign, capture:

1. repository root and HEAD in one `git rev-parse --show-toplevel HEAD` process;
2. `git status --porcelain -z` (NUL paths avoid filename quoting differences);
3. for each path returned by status only, `(XY status, mtime_ns, size)` via
   `Path.lstat()`.

At done, take the same porcelain/path-metadata view and retain only paths whose
entry differs between snapshots. The comparison is symmetric, so it includes a
new dirty path, metadata/status changes, and a baseline-dirty path that
disappeared. An unchanged pre-existing screenshot has an identical entry and is
excluded. These filtered dirty paths are unioned with the existing committed
HEAD-interval diffstat.

This was chosen over removing/renaming the field because it fixes the concrete
false attribution without hashing or walking the tracked tree. Work is
O(number of porcelain paths), not O(repository files). Assign adds one status
process; done uses one porcelain result for both the whole-tree uncommitted
count and the interval comparison instead of running status twice.

## Honesty and failure behavior

- `None` means the baseline/status could not be measured; `{}` means it was
  measured and clean. A failed done-time status produces `ไฟล์ที่แตะ:ตรวจไม่ได้`
  rather than treating an empty error result as a clean current tree.
- The note now says the shared-tree value compares assign-time HEAD plus dirty
  path/mtime/size, and may still include another pane's changes made during the
  same interval. Metadata cannot prove process authorship in a truly shared
  checkout.
- Metadata is compared for equality, never with `>`/`<`. This avoids assuming
  a timestamp resolution or ordering specific to Windows, macOS, or a
  filesystem. `lstat` also avoids following checkout symlinks.
- A content change that preserves both size and mtime and leaves the same git
  status is intentionally not detected; avoiding content hashes is the
  performance constraint accepted by option (ก).

## Isolation and lifecycle

- Isolated worktrees do not populate the new shared-tree fields and keep the
  existing `WorktreeInfo.base_sha`/diffstat/finalization flow unchanged.
- The checked porcelain probe is a separate shared-tree API, so a status
  failure is distinguishable from a clean tree; the established worktree
  `status_porcelain()` behavior is unchanged.
- Provider output is not inspected. The state lives in provider-neutral
  `PaneState`, so Claude, Codex, Gemini, OpenCode, Kimi, and Cursor use the same
  path.
- Stuck close/respawn recovery preserves the git root and dirty snapshot along
  with the assign-time HEAD, so resuming the same task does not lose its
  baseline.

## Targeted verification

Run with the repository venv at
`C:\Users\monch\WebstormProjects\agent-takkub\.venv\Scripts\python.exe` and
`PYTHONPATH=<this-worktree>\src` (the shared venv editable install points to the
main checkout):

```text
python -m ruff check <8 touched Python files>
All checks passed!

python -m pytest -q \
  tests/test_digest_facts.py \
  tests/test_worktree_manager.py \
  tests/test_done_digest_facts_wiring.py \
  tests/test_done_evidence.py \
  tests/test_lifecycle_recovery.py
216 passed
```

Coverage includes unchanged pre-existing dirty paths, mtime/size/status
changes, newly dirty and disappeared paths, clean-vs-failed snapshot semantics,
the issue's stale-screenshot done-wiring reproduction, worktree isolation,
assign capture cost (two git processes), and stuck-recovery preservation.
