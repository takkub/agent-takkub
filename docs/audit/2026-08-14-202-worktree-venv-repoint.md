# #202 — worktree pane repoint shared .venv editable install

**Severity:** high · **Fixed by:** backend (`wt/backend-3-1786718976`) · **Date:** 2026-08-14

## Incident

A `backend` pane working in `--isolation worktree` ran `pip install -e .` from
inside its own worktree checkout. That rewrote the **shared** dev-checkout
venv's `__editable__.agent_takkub-1.0.60.pth` (in `.venv/Lib/site-packages`,
used by every pane's `python`/`takkub` in this repo) to point at the
worktree's `src/` instead of the main repo's. Once the Lead removed that
worktree after merging, `.venv`/`takkub` broke cockpit-wide
(`ModuleNotFoundError`). Worse: while the pane was still alive, every other
process sharing that venv — including a `qa` full-suite run that overlapped
in time — silently imported code from the wrong worktree, so that gate's
result can't be fully trusted. Lead's stopgap fix:
`pip install -e . --no-deps` from repo root.

## Root cause

`pane_guard.py`'s classify() had no rule for `pip install -e`/`--editable` —
nothing stopped a teammate pane from repointing a venv that every sibling
pane (and `takkub` itself, in a dev checkout) shares. `doctor.py` had no
check that could catch a stale/misdirected `.pth` after the fact, and
worktree removal (`merge_isolated`/`clean_isolated`/`safe_remove`/
`force_remove`) never checked whether the checkout it just deleted was the
thing the shared venv's editable install pointed at.

## Fix (5 layers, matches the issue's own priority order)

1. **`pane_guard.py`** — new `pip_editable` rule blocks `pip install -e` /
   `--editable` (any target, any pip invocation form) for every guarded
   role, no allowlist — mirrors `host_destructive` (#169)'s "no legitimate
   use" shape. Anchored to actual command-start position (`_CMD_START`) so
   `echo`/`grep` mentioning the phrase don't false-positive. New
   `PIP_EDITABLE_RULE_TEXT` constant, mirrored into all 16
   `.claude/agents/*.md` role files (pinned by a new
   `tests/test_agent_role_files_have_pip_editable_guard.py`, same pattern as
   the browser/host-destructive guard tests) — this is what a non-claude
   pane (codex/gemini-agy/opencode/kimi/cursor) actually sees, since Claude
   Code hooks are claude-only (#103).
   - This is item 2 from the issue. **Item 1** ("worktree isolation ต้องแยก
     venv จริง") was evaluated and rejected for this pass: a real per-worktree
     venv (`python -m venv` + reinstall) adds seconds to every isolated
     spawn regardless of whether the pane ever touches Python, for a project
     where most isolated panes (frontend/mobile/devops work) never would.
     The pane_guard block + the safe alternative it points to (plain
     `pytest`, or `PYTHONPATH=<worktree>/src` to override the shared
     install without mutating it — used throughout this very fix's own test
     runs) delivers the same practical isolation at zero spawn cost.
2. **`doctor.py`** — new `check_editable_install()` (`[venv]` category, dev
   checkout only — no-op when `DATA_HOME != REPO_ROOT`, same guard
   `check_installed_integrity` uses). Reads every
   `__editable__.agent_takkub-*.pth` in `.venv/site-packages`;
   FAILs (with `auto_fix` wired to `pip install -e . --no-deps` from
   `REPO_ROOT`) when the target doesn't exist or contains a `worktrees`
   path segment; WARNs when it points somewhere else unexpected; OK when it
   matches `REPO_ROOT/src`. Wired into `run_all_checks()`.
3. **`worktree_manager.py`** — new `repair_editable_pth_if_stale(git_root,
   removed_path)`: after a worktree checkout is gone, checks whether the
   dev-checkout venv's editable `.pth` pointed AT (or under) the just-removed
   path, and if so re-runs `pip install -e . --no-deps` from `git_root`
   automatically. Wired into all four removal paths — `safe_remove`,
   `force_remove`, `merge_isolated`, and `clean_isolated`'s per-row removal
   — so this incident self-heals the moment it would otherwise have bitten,
   regardless of which command did the removing.
4. Same as (3) — `takkub worktree merge`/`takkub worktree clean` are the two
   removal paths actually wired into the CLI; both call through
   `WorktreeManager`, so the repair fires for both without extra CLI-layer
   changes.
5. **`tests/conftest.py`** — `_assert_agent_takkub_matches_this_checkout()`
   runs at collection time, before any test executes: imports `agent_takkub`
   (no-op if it's not importable at all — the `installed-gate` CI job
   deliberately runs without it) and raises immediately if
   `agent_takkub.__file__` doesn't resolve under *this* checkout's own
   `src/`. Verified live in this exact worktree: running the suite without a
   `PYTHONPATH` override reproduces the failure mode this issue is about
   (shared venv resolves to the main repo's `src/`, not this worktree's) —
   the new assertion catches it immediately instead of silently testing the
   wrong code.

## Verification

All new/changed code was tested via `PYTHONPATH=<this-worktree>/src` against
the main repo's shared `.venv` interpreter — **never** via `pip install -e .`
from this worktree, which would have reproduced the exact incident being
fixed. Targeted suites, all green:

```
tests/test_pane_guard.py
tests/test_agent_role_files_have_pip_editable_guard.py   (new)
tests/test_agent_role_files_have_host_destructive_guard.py
tests/test_agent_role_files_have_browser_guard.py
tests/test_doctor.py           (+ TestCheckEditableInstall, new)
tests/test_worktree_manager.py (+ TestRepairEditablePthIfStale, new)
tests/test_cli_guard.py
```

`ruff check` clean on every touched file. Full suite intentionally NOT run
here (`main` full-suite pass is qa's batch gate, not a mid-flight backend
task — see CLAUDE.md test-tier policy); this change touches no other
subsystem's runtime path, only guard/doctor/worktree-lifecycle code exercised
by the suites above.

## Follow-ups NOT done in this pass

- No automated repair for a venv **already** broken by a worktree removed
  *before* this fix landed — `takkub doctor --fix` (item 3's `auto_fix`)
  covers that manually.
- Item 1's "real per-worktree venv" was deliberately not built (see above) —
  flag to Lead if a future project's worktree panes need heavier Python
  isolation than the guard + `PYTHONPATH` pattern gives.
