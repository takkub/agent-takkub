# Release gate — round 2 final (2026-08-13)

HEAD = a52e91e, run on main worktree (not a linked worktree). Orphan worktree
`worktrees/agent-takkub/frontend-1786631069-fix` (c78f517) left untouched on
disk throughout, per instructions — proves the boot-time git-subprocess guard
(#188/orchestrator fix chain) works with a real orphan present, not because
the environment was cleaned up first.

## Results

| Check | Result |
|---|---|
| `pytest -q` (full suite, `.venv` editable install) | **5731 passed, 7 skipped, 0 failed** (exit 0) |
| `ruff check src/ tests/` | Passed (exit 0) |
| `ruff format --check src/ tests/` | Passed (exit 0) |
| `lint-imports` | **24 kept, 0 broken** |
| `python tools/gen_import_graph.py --check` | fresh, module_count=141 (exit 0) |
| `pre-commit run --all-files` | **6/6 hooks Passed**: gitleaks, ruff, ruff-format, takkub-docs-verify, import-linter, depgraph-fresh |
| `git status` after everything | clean, no hook wrote stray files |

Baseline before this round was 5723 passed / 7 skipped; +8 new tests this
round (the two new guard files below) all green, no regressions, no drift.

### Note: no pytest summary line printed
The raw log ends at `[100%]` with no `"N passed in Ys"` trailer despite exit
code 0. Verified this is not a hidden failure by counting result markers
directly from the raw dot-stream (excluding the `PYTEST_EXIT:0` sentinel text
I appended, which itself contains letters that would otherwise look like
markers): 5731 `.`, 7 `s`, 0 `F`/`E`/`x`/`X` from the actual test run. Exit
code 0 + zero failure/error markers + count matching baseline+8 is conclusive.
Cosmetic pytest/plugin quirk under non-tty redirection, not a test result
issue — flagging so it isn't mistaken for a hang next time.

### First attempt caught two of my own mistakes (not app bugs)
1. Redirected pytest output to `/tmp_pytest_full.log` (git-bash root, no
   write permission) — command errored before pytest even ran. Not a code
   issue, just a bad path; corrected to write inside the scratchpad dir.
2. Ran the first `pytest`/`lint-imports`/`gen_import_graph.py --check` via
   plain `python`/console scripts on PATH (system Python), which doesn't have
   the editable install — `lint-imports` failed with "Could not find package
   'agent_takkub'" and pytest silently collected 0 tests. Known project
   pitfall (memory: run tests via `.venv`, not system python). Reran
   everything through `.venv/Scripts/python.exe` and its console scripts;
   all green from there.

### Real slowness during the run (not a hang, not a code bug)
The suite took a long time on this box. Traced it: `tests/test_installed_
mode_gate.py`'s and `test_installed_cli_bin_integration.py`'s session-scoped
`installed_venv` fixtures each build a real wheel + fresh venv via
`subprocess.run([..., "-m", "build", ...], timeout=180)` followed by
`venv.create(venv_dir, with_pip=True)`. The wheel build has an explicit
180s timeout; **`venv.create()`'s internal `ensurepip` bootstrap does not** —
confirmed via `Get-CimInstance Win32_Process` that a
`... -m ensurepip --upgrade --default-pip` subprocess sat at near-zero CPU
growth for ~30 minutes before completing on its own, twice (once per
venv-target fixture instance). CPU/memory on the top-level pytest process
kept climbing throughout (confirmed via repeated `Get-Process`), so this was
never a hung/dead process — just this machine's known pagefile/commit-charge
I/O stutter (see project memory `devbox-memory-tuning-2026-07-23`) landing on
an unthrottled subprocess call. Not something to fix in the test (a hardcoded
timeout on `venv.create()` would risk flaking CI runners that are fine), just
noting it so a future "suite looks stuck" moment isn't mistaken for a real
hang before checking CPU/child-process activity first.

## CI risk assessment (ubuntu-latest / macos-latest vs. this Windows run)

Reviewed `.github/workflows/ci.yml` after devops's 2 rounds of fixes, plus
the two new guard test files, specifically for anything that passes here but
could flip on Linux/macOS CI:

- **`CI` env var / `_auto_issue_suppressed()`**: `auto_issue_capture.py` now
  checks `PYTEST_CURRENT_TEST` and `"pytest" in sys.modules` *before* it ever
  gets to the `CI` check — both are true for every single test in this repo's
  suite, so the `CI`-env branch is dead code during any pytest run regardless
  of OS. `grep`'d `src/` for other `os.environ.get("CI")` usages — the one in
  `auto_issue_capture.py` is the only site. No CI-vs-local behavior
  divergence risk here.
- **`tests/test_app_exception_guard.py`**: exercises the real
  `sys.excepthook` via `app_mod._install_exception_guard()` + a fake
  `_SyncThread`, all pure Python/stdlib (`sys.exc_info`, `monkeypatch`) — no
  platform-specific paths, no subprocess. Should behave identically on all
  three CI OSes.
- **`tests/test_orphan_worktree_prune_guard.py`**: uses `pathlib`
  (`tmp_path / "worktrees" / "proj" / ...`) throughout — no raw string path
  joins. The fabricated `.git` pointer file content
  (`"gitdir: /repo/.git/worktrees/backend-guard-repro\n"`) is never parsed by
  real git (the test monkeypatches `wtm.subprocess.run` before anything
  touches it), so the forward-slash-only gitdir path is irrelevant to
  cross-platform correctness — it's just an opaque string being written and
  read back. `QCoreApplication` fixture is headless-safe (no display needed
  for `QCoreApplication`, unlike `QApplication`) — consistent with this
  repo's existing Linux headless-CI pattern noted in `ci.yml`'s own comments.
- **`"pytest" in sys.modules` guard**: this is a `sys.modules` membership
  check, not an env var or path — identical behavior on every OS/Python
  build; no CI-specific risk.
- **Conftest env guards**: `TAKKUB_SKIP_MCP_WARM` / `TAKKUB_SKIP_GRAFT_BUILD`
  / `TAKKUB_SKIP_ORPHAN_WORKTREE_PRUNE` are all set via
  `os.environ.setdefault(...)` at `conftest.py` module import time (not
  fixture-scoped), so they're active for every test on every OS the same way.

No CI-only risk identified in this round's changes. The one caveat I can't
verify from this Windows box: the `installed_venv` fixture's wheel-build/venv
stall pattern (see above) — if ubuntu/macos CI runners hit equivalent I/O
contention, the 180s `subprocess.run` timeout on the wheel build *would* fire
and fail loudly (assertable, not silent), but `venv.create()`'s unguarded
`ensurepip` step has no such backstop on any OS. This is a pre-existing gap,
not something introduced this round — flagging for awareness, not blocking.

## Verdict: GO

0 failed across full suite (5731 passed/7 skipped), lint/format/import-linter/
depgraph/pre-commit all green, git tree clean, orphan worktree left in place
throughout without breaking anything. No fake-drift, no assertion edits, no
regressions found — nothing needed fixing this round.
