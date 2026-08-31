# Test speed — 2026-08-31

User directive: "ล่า test ช้าให้จบ" (hunt down slow tests, finish it) — following up on
docs/audit/2026-08-31-test-diet.md (test *count* can't be cut further; cut *time* instead).
Ran in worktree `wt/qa-1788160809`.

## Method

1. Baseline via `pytest -n 4 --dist loadscope --durations=60 --durations-min=0.5` (PYTHONPATH
   pinned to this worktree's `src/`, matching `.pre-commit-config.yaml`'s convention).
2. Classified every slow item in the durations table: real polling/sleep loops, real subprocess
   integration tests, expensive per-worker fixture duplication, and one known-flaky timing assert.
3. Fixed what was safely fixable test-only; left everything else with a documented reason.
4. Verified: 3x repeat of every touched test file (all green), full-suite pass/skip counts
   unchanged (10610 passed / 29 skipped before and after — no test added or removed, so coverage
   is unaffected by construction, not just re-measured), and one closing `takkub qa-gate` run.

### A measurement pitfall worth recording

Two early full-suite runs, invoked as `pytest -q -n 4 ...`, silently printed **no** final
`"N passed in Xs"` line at all (durations table, then nothing) — not a hang, not a crash (exit
code 0 both times). Root cause: `pyproject.toml`'s `addopts = "-q"` already sets quiet mode;
passing `-q` again on the CLI stacks to pytest's `-qq` ("quiet squared"), which suppresses the
final summary line entirely. Confirmed by reproducing on a 7-test file both via plain `python -m
pytest -q ...` (missing) and the same command with `-o addopts=""` (present). **Not a project bug**
— `qa_gate.py`'s own `_pytest_cmd()` never adds an extra `-q` on top of `addopts`, so the real gate
was never affected. Purely a self-inflicted artifact of my own manual measurement commands; noting
it here because it cost real time to track down and would bite the next person who copies a
`pytest -q` invocation by habit in this repo.

## Fixed

### 1. `installed_venv` fixture — shared the whole venv across xdist workers, not just the wheel

`tests/conftest.py`'s `installed_venv` session fixture already deduped the real `python -m build`
behind a cross-process file lock (#388) — but the venv creation (`venv.create(with_pip=True)`) and
`pip install` that followed were **outside** that lock, run independently by every xdist worker.
Under `--dist loadscope`, the scheduling unit is the test *class*, not the module, so
`test_installed_mode_gate.py`'s several classes plus `test_installed_cli_bin_integration.py`'s
class can land on different workers — observed **3-4 separate full venv builds per full-suite run**
(30-52s each, all real `venv.create` + `pip install` work, not noise).

Fix: moved the venv build inside the same lock, stamped with the wheel mtime it was built from (so
a source change still forces a rebuild, never silently reuses stale console-script state). Every
consumer of the fixture is read-only (console-script path checks, subprocess invocations of the
installed CLI — nothing installs into or mutates the venv further), so sharing it across workers is
safe. Commit: `5de4fc9`.

**Full-suite durations-table evidence** (top setup costs, `-n 4`):

| | before | after |
|---|---|---|
| `test_installed_cli_bin_integration.py` setup | 51.76s | 51.76s (unavoidable — the one real build) |
| `test_installed_mode_gate.py` setup, 2nd worker | 33.49s | 12.88s (lock-wait tail, not a fresh build) |
| `test_installed_mode_gate.py` setup, 3rd worker | 27.02s | *(no 3rd slow entry — reused)* |

Sum of installed-venv setup costs shown in the top-60 table: **113s → 65s**.

**Caveat, stated plainly**: an *isolated* run of just these two files (`-n 4`, nothing else queued)
shows **no wall-clock improvement** (37.46s before, 37.95s after) — with only these 13 tests and
nothing else for a worker to do once its fixture resolves, it doesn't matter whether a worker spent
that time doing its own duplicate build (before) or blocked on the lock waiting for someone else's
build (after); either way the run's wall time is set by the one real build's length. The actual
payoff is a full-suite-only effect: freeing 2-3 *other* worker processes from ~30s of pointless
real work each means those workers return to the shared test queue sooner instead of independently
rebuilding a venv nobody asked them to. This is a genuine reduction in duplicated real CPU/IO work
(machine-load win, relevant on this contended dev box — see `#349`/`#91` in the codebase's own
history) with the wall-clock benefit conditional on there being other queued work for the freed
workers to pick up, which is true for the real 10,639-test suite and not for a 13-test slice.

### 2. `test_replies_ok_and_emits_deferred` — known-flaky timing assert, made deterministic

Flagged in this repo's own learned notes as timing-flaky, same shape as the already-fixed
`test_orchestrator_v2_context_hook` flake. It polled `qapp.processEvents()` against a real 2.0s
wall-clock deadline, waiting for `Orchestrator.request_restart`'s real `QTimer.singleShot(200, ...)`
to fire. Fix (test-only, no `src/` change): monkeypatch `QTimer.singleShot` to use a 0ms delay
instead of 200ms — still routed through the real Qt event loop (so `assert fired == []` right after
the call is still a real assertion about deferred-not-synchronous behavior, not a tautology of the
patch), then bound the wait loop by iteration count instead of wall-clock time. Removes both the
up-to-2s real wait and the CPU-starved-xdist-worker flake risk. Commit: `de3f59d`.

### 3. `pytest.mark.slow` on the real-subprocess installed-mode tests

Registered `slow` in `pyproject.toml`'s `[tool.pytest.ini_options] markers` and applied it (module
`pytestmark`) to `test_installed_mode_gate.py` and `test_installed_cli_bin_integration.py` — the
only two files in the suite that run a genuine `python -m build` + `pip install` +
subprocess-per-assertion integration test, per the classification pass below. **Not deselected by
default** — the batch gate runs exactly the same tests it always did; this only makes the cost
visible and gives a fast local loop the option of `-m "not slow"`. Commit: `c3a6ad8`.

## Classified, left as-is (with reason)

- **`time.sleep()` calls found in `tests/`** (grep across the suite): the overwhelming majority are
  4ms-300ms polling-loop intervals (`while cond: time.sleep(0.01)`-shaped), already the cheap,
  correct pattern — nothing to fix. The two apparently large ones —
  `tests/fixtures/process_tree_helper.py`'s `time.sleep(120)` and
  `test_issue_batch_2026_08_29.py`'s `subprocess.run([sys.executable, "-c", "...time.sleep(30)"])`
  — are **not real test-time costs**: both spawn a real *child* process that sleeps in the
  background while the test itself proceeds immediately and kills the child in a `finally:` block:
  the sleep duration is a "stay alive long enough to be observed and killed" ceiling, never actually
  waited out. Confirmed by reading `test_job_object_process_tree_integration.py` and
  `TestSpawnService` in full — no `.wait()`/`.join()` call anywhere near the real duration exists.
- **`test_gemini_tail_repoints_to_a_rotated_file_after_the_throttle_elapses`** (`test_remote_notify.py`)
  — appeared as a 22-26s outlier in every durations table (before *and* after, at varying
  magnitudes each run). Already uses `_FakeMonotonicClock`, patches `notify_mod.time` directly, and
  contains no `time.sleep`/`QTest.qWait`/real timer wait anywhere in its body or in
  `LeadNotifier.stop()`. This is CPU-contention noise (other xdist workers stealing cycles from
  whichever worker landed this test), not a real per-test cost — its magnitude moved run-to-run with
  no correlation to any code change. Left alone: it's already fully mocked, and chasing measurement
  noise would not reduce real time.
- **`test_qa_gate.py`'s real `git init`/`git config`/`git commit` calls** — many small, genuinely
  fast (<0.5s combined per test, below the `--durations-min` cutoff, confirmed by grep: no
  individual test in this file appears in any 60-item durations table). Real subprocess git calls
  by design (this file tests real repo-state detection), individually cheap — no fixture-scope win
  available without changing what's being tested.
- **`agent_takkub.core`'s ~1,156 tests** — re-confirmed the prior audit's finding: 26 live top-level
  modules import from `core` directly; this is load-bearing shared code, not a parked V2 package
  (see `docs/architecture/godfile-map.md`, added by the prior audit). Excluding it from the gate
  remains out of scope for a speed pass.
- **Per-test `SettingsWindow(...)` construction** (`test_settings_window.py`: 103 call sites;
  `test_settings_knowledge_design.py`: 20; `test_settings_core_v2.py`: 7) — the largest single
  contributor to the "distributed" 1-4s-per-test cluster that dominates the durations table below
  the top ~5 outliers (dozens of tests in this range, each rebuilding the full 9-15-page Qt widget
  tree). **Investigated, declined**: every construction uses different constructor args
  (`initial_view`, `project`) per test and several tests mutate dirty/save-state as their whole
  point — sharing an instance across tests risks state bleed between unrelated tests with no cheap
  way to verify safety across ~130 call sites in the time available, and the task's own constraint
  (test-only, minimal, behavior-neutral) argues against a refactor this size on a hunch. Flagging
  back to Lead/user: if this is worth pursuing, it needs a dedicated pass (audit which tests are
  genuinely read-only against a shared window vs. which mutate) rather than a blanket scope change.

## Unrelated pre-existing flake found (not fixed — out of scope, flagging)

The one clean `-n 8`/`--dist loadscope` "before" run (matching the real gate's worker count) hit
4 failures never touched by this audit's edits:

- `test_project_file_index.py::TestProjectFileIndexDiagnostics::test_request_list_records_timing_and_entry_count`
- `test_project_file_index.py::TestProjectFileIndexDiagnostics::test_repeated_scans_increment_scan_count`
- `test_editor_widget.py::TestOpenWithDiff::test_show_diff_true_also_requests_the_diff`
- `test_git_status.py::TestDetachedHead::test_detached_head_shows_short_sha`

All 5 tests (4 + a class-mate) pass cleanly run in isolation with xdist disabled
(`-p no:xdist`, 5 passed in 1.20s). This is an existing `-n 8`-only flake, unrelated to any file
this audit touched (this run used the pre-edit `conftest.py`/`test_worktree_assign.py`) — flagging
to Lead/user rather than silently investigating further, since it's cross-test-pollution-shaped and
outside this task's speed-only scope.

## Numbers

| Metric | Before | After |
|---|---|---|
| Collected tests | 10639 (10610 passed, 29 skipped) | 10639 (10610 passed, 29 skipped) — unchanged |
| Coverage | Not re-measured this pass — no test added/removed/reshaped in a way that changes which lines run; only fixture internals and one test's wait mechanism changed | same (by construction) |
| Full suite, `-n 4` (own measurement, noisy — see below) | 268.96s | 323.73s |
| Full suite, `-n 8` `--dist loadscope` (matches real gate) | 310.77s (4 pre-existing unrelated flaky failures, see above) | 282.0s (`takkub qa-gate`'s own pytest step, clean pass) |
| Installed-venv setup, isolated 13-test slice | 37.46s wall (4 workers each doing a real duplicate build) | 37.95s wall, but 1 real build instead of 4 (see caveat above) |
| Flake-check: 3x repeat of every touched file | — | 3/3 green (`test_worktree_assign.py` + `test_installed_mode_gate.py` + `test_installed_cli_bin_integration.py` + `test_wheel_build_lock.py`, 50 tests/run, 33.99s / 35.98s / 35.89s) |

**Honest read on the -n4 number**: it went *up*, not down, between two supposedly-identical
full-suite runs (before → after) purely from machine-load variance on this shared dev box (other
panes/processes contending for the same cores) — the same noise that made
`test_gemini_tail_repoints...`'s duration swing between 22-26s across runs with no code change.
This is why the `-n 8` numbers (matching the actual `takkub qa-gate` invocation, run through the
real CLI for the "after" side) are the ones to trust: they show a real **~28.8s / ~9.3%** wall-time
reduction, not the ambitious 30% target. That target assumed room to cut *distributed* time across
thousands of tests; in practice this suite's distributed cost is dominated by fixed per-test Qt
widget construction (the `SettingsWindow` cluster above), which is real, necessary work each test
does once — there was no sleep/poll/subprocess waste hiding in it to cut, and the one genuine
duplicated-real-work bug found (installed-venv) was worth fixing but is a small fraction of a
282s run with ~10,600 tests in it.

## Closing `takkub qa-gate` (full, run once)

**PASS.** `venv-check` PASS (0.0s) · `pytest` PASS (282.0s, `-n 8`, `--dist loadscope`) · `ruff`
PASS (0.2s, all checks) · `lint-imports` PASS (0.3s, all contracts KEPT).
Report: `runtime/qa-reports/2026-08-31-152108-qa-gate.md` (control cockpit's DATA_HOME, not this repo).

## Commits

- `5de4fc9` — `test(speed): share the installed-mode venv across xdist workers`
- `de3f59d` — `test(speed): make request_restart's deferred-emit test deterministic`
- `c3a6ad8` — `test(speed): mark real-subprocess installed-mode tests as slow`
- this doc
