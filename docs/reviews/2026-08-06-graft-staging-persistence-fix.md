# graft H1 follow-up — persistent staging mirror (2026-08-06)

**Context:** H1 (2026-08-05 cross-OS audit, `docs/reviews/2026-08-05-graft-crossos-audit.md`)
fixed the unbounded-gitignored-bulk problem by staging only `git ls-files`-reported
non-ignored files into a tempdir before `graft build`. Lead re-tested after that fix
landed and found the store balloon right back (41MB/413 files → 435MB) the moment a
single `graft ask`/`graft mcp` query ran against it.

## Root cause

`_run_build`'s tempdir staging copy was deleted in a `finally: shutil.rmtree(...)`
right after the build subprocess exited. graft's own per-query freshness gate
(`ensureFreshGraph` in the shipped `graph/refresh.js`) re-probes the working tree at
whatever `dir` argument the CURRENT call receives — `graft ask <query> [dir]` and
`graft mcp [dir]` both default `dir` to `"."` (the calling process's own cwd) when not
given explicitly. A pane's injected `graft mcp` server (`shared_dev_tools.
browser_profile_mcp_config_path`) was never given a positional `dir` at all, so it
defaulted to the pane's own cwd — the real, unfiltered target directory.

Two independent gaps combined:

1. The staged copy graft was actually built from no longer existed by the time any
   query ran.
2. Even if it did, nothing pointed the query's `dir` at it — every query resolved
   `dir` against the pane's real cwd instead.

Either gap alone is enough to make `ensureFreshGraph` see "this graph doesn't match
this root" and silently rebuild — unfiltered, since graft has no `.gitignore` support
of its own (H1's whole premise). Verified against the real CLI in a scratch repo:
building into a copy that's then deleted, then querying with the default `dir="."`,
reproduces `[graft] refreshed the graph (N files changed) before answering` with N =
every file under the real target, gitignored or not.

## Fix

Make the staging mirror **persistent** and thread it through consistently as the
explicit positional `dir` on every build AND every query:

- `graft_store.staging_dir_for(target)` — new store-shaped root
  (`GRAFT_STAGING_ROOT`), a true sibling of both *target* and the graph store
  (never nested in either, to avoid reproducing the self-ignoring-`.gitignore`
  bug M5 already found for a store nested inside its own target).
- `graft_autobuild._sync_staging` — re-syncs the mirror on every build: removes
  files no longer in the current non-ignored set (deleted/renamed/newly-ignored),
  then re-stages the rest. `_stage_files` now unlinks before hardlinking/copying so
  a changed file's content is picked up even when an editor's rename-over-write save
  gives it a new inode.
- `shared_dev_tools.browser_profile_mcp_config_path` — appends the staging mirror
  path as the positional `dir` after `"mcp"`, alongside the existing `--dir <store>`.
- `disk_usage.scan_graft_graphs` / `_prune_graft_graphs` — fold the staging mirror's
  bytes into its paired store's entry (same `graph_key`) and delete both together on
  prune, so the new persistent directory has the same visibility/reclaim path H1(c)
  already gave the store.

## Verification (this session, real repo + real graft CLI)

- Built this repo's own graph via the new code path: store 43,033,065 bytes / 413
  files, staging mirror 20,916,244 bytes / 736 files (736 > 413 because staging holds
  every non-ignored file `git ls-files` reports, not just the extension-filtered
  subset graft parses as code).
- Confirmed hardlinks, not copies: staged file and source file share the same inode
  (`st_ino` equal, `st_nlink=2`) — real incremental disk cost is ~0 on this same-volume
  machine, the naive byte-sum accounting above just double-counts shared blocks (an
  existing property of `_dir_stats` everywhere else in this codebase, not new here).
- Queried through the REAL `graft mcp` stdio server (JSON-RPC `tools/call
  graft_find_code`), not just the `ask` CLI: response contained no `[graft] refreshed`
  note, and store+staging byte counts were byte-for-byte identical before and after
  the query.
- Confirmed the relative path graft returned (`src\agent_takkub\mcp_bridge.py:L322`)
  opens to identical content from the pane's real cwd (the actual target repo, not the
  staging mirror) — proves an agent can actually follow the paths graft hands it.
- Full pytest suite green, ruff clean, all 23 import-linter contracts kept.

## Trade-off accepted

Freshness now compares against the staging mirror, not the live target directly, so
an edit made between rebuild triggers (boot / tab-switch / `done()`'s debounced
rebuild) is invisible to a query until the next trigger resyncs the mirror. This is
the same staleness window `graft_autobuild.py`'s debounce already accepted for the
graph itself — this just extends it to the one directory graft's own freshness check
reads from.
