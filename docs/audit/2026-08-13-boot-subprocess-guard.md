# Boot-time subprocess guard #3 — orphan-worktree sweep (2026-08-13)

## Bug

`Orchestrator()` construction spawned a real `git` subprocess:

```
AssertionError: Orchestrator() construction spawned subprocess.run calls: [(['git', '-C',
'C:\\Users\\monch\\WebstormProjects\\agent-takkub\\worktrees\\agent-takkub\\frontend-1786631069-fix',
'rev-parse', '--show-toplevel'],)]
```

Failing tests (red on any machine with a worktree checkout on disk):
- `tests/test_graft_autobuild.py::test_orchestrator_construction_spawns_no_graft_subprocess`
- `tests/test_mcp_warm_guard.py::test_orchestrator_construction_spawns_no_subprocess`

## Call chain

```
orchestrator.py:735  prune_orphan_worktrees_boot()
  -> disk_usage.py:1135/1154  classify_worktree()
    -> disk_usage.py:265  mgr.git_root()
      -> worktree_manager.py:382  self._run([...'rev-parse','--show-toplevel']) = real subprocess.run
```

## Root cause

`Orchestrator.__init__` has three boot-time subprocess/network sources:

1. `warm_browser_mcps()` — guarded by `TAKKUB_SKIP_MCP_WARM` (#91)
2. `build_all_projects_async()` / `warm_graft_mcp()` — guarded by `TAKKUB_SKIP_GRAFT_BUILD`
3. `prune_orphan_worktrees_boot()` — **not guarded at all**, introduced in `ce05d26` (2026-07-27)

CI never caught this because the GitHub runner never has a leftover worktree
checkout on disk (`find_worktree_dirs()` finds nothing → no-op). It only
reproduces on a dev machine that has one, which is exactly this checkout's
`worktrees/agent-takkub/frontend-1786631069-fix` — a genuine latent bug, not
a regression from this session's other changes.

## Fix

- `disk_usage.prune_orphan_worktrees_boot()` now checks a third kill switch,
  `TAKKUB_SKIP_ORPHAN_WORKTREE_PRUNE` (same "" / "0" = active semantics as
  the other two), and returns `0` immediately when set — guarded inside the
  function itself (not just at the `orchestrator.py` call site) so every
  caller is covered, matching the existing two guards' rationale.
- `tests/conftest.py` sets `TAKKUB_SKIP_ORPHAN_WORKTREE_PRUNE=1` for every
  test the same way it already does for the other two (module-level
  `setdefault` + per-test `monkeypatch.setenv` in the autouse fixture). No
  function-level monkeypatch stub was added — this guard has no
  fire-and-forget background thread to stand in for, so the env check alone
  is the full guard (same shape as the pre-existing `TAKKUB_SKIP_NATIVE_CHROME`).
- `tests/test_disk_usage.py::TestBootSweepConservative` (4 tests) call
  `prune_orphan_worktrees_boot()` directly to exercise its real sweep
  behavior — each now does `monkeypatch.delenv("TAKKUB_SKIP_ORPHAN_WORKTREE_PRUNE",
  raising=False)` first, same pattern `test_graft_autobuild.py` already uses
  throughout for `TAKKUB_SKIP_GRAFT_BUILD`.
- New `tests/test_orphan_worktree_prune_guard.py` — 3 tests:
  - `test_prune_orphan_worktrees_boot_noop_when_env_set` — guard on, real
    orphan-worktree fixture dir present, asserts zero subprocess calls.
  - `test_prune_orphan_worktrees_boot_scans_when_env_unset` — guard off,
    same fixture dir, asserts a real `git ... rev-parse` call IS made —
    proves the first test isn't vacuous (i.e. that the fixture dir actually
    reaches the subprocess call site when nothing suppresses it).
  - `test_orchestrator_construction_spawns_no_worktree_subprocess` — the
    actual regression test: patches `disk_usage.DATA_HOME` to a tmp dir
    holding the fabricated orphan-worktree fixture (so the repro doesn't
    depend on a real leftover worktree being present on whichever machine
    runs the suite), constructs a real `Orchestrator()`, asserts zero
    subprocess calls, relying only on the production env-guard (conftest's
    env var — no function-level stub exists for this guard to fall back on).

## Other boot-time subprocess sources checked

Walked `Orchestrator.__init__` (orchestrator.py:647-900) end to end. The
three `ensure_browser_mcps()` / `ensure_graft_mcp()` / `ensure_user_mcps()`
calls are pure JSON file merges (`shared_dev_tools.py`) — no subprocess or
network I/O. `prune_old_transcripts()`, `prune_old_browser_profiles()`, and
the `task_ledger` reconciliation loop are filesystem-only. No further
unguarded subprocess/network call sites found.

## Verification

```
.venv/Scripts/python.exe -m pytest tests/test_graft_autobuild.py tests/test_mcp_warm_guard.py \
  tests/test_disk_usage.py tests/test_worktree_manager.py tests/test_orphan_worktree_prune_guard.py -q
# 182 passed, exit 0 — including the real leftover worktree checkout still on disk

.venv/Scripts/python.exe -m ruff check <touched files>       # All checks passed!
.venv/Scripts/python.exe -m ruff format --check <touched files>  # 4 files already formatted
.venv/Scripts/lint-imports.exe                                # Contracts: 24 kept, 0 broken
```

Cross-platform: the guard is a plain `os.environ` string check, no
platform-specific paths. Multi-provider: the guard is provider-neutral (it
gates a pane-provider-agnostic boot sweep, not any single CLI's spawn path).

The leftover worktree at `worktrees/agent-takkub/frontend-1786631069-fix`
was left untouched, as instructed, and is what proves the fix is real (the
two originally-failing tests are green with it still present on disk).
