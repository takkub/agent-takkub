# CI determinism investigation — the 3 pre-existing test failures

Date: 2026-08-13
Branch: `wt/qa-1786609656`
Trigger: user directive "CI ต้องเขียวเท่านั้นถึง publish version ใหม่", prior QA gate
(`docs/qa/2026-08-13-autoskills-newrole-gate.md`) reported 3 pre-existing failures
suspected to be pytest-randomly seed-dependent flakiness.

## 0. Environment

Fresh venv, editable install:
```
python -m venv .venv-qa
./.venv-qa/Scripts/python.exe -m pip install -e ".[dev]"
```
Verified editable install resolves into `src/` (not a stale wheel).

## 1. Correcting the premise: pytest-randomly is NOT used in this repo

The task assumed `pytest-randomly` shuffles test order per-seed and that the 3
failures are seed-dependent. This is **factually wrong** — verified, not assumed:

- `pyproject.toml`'s `[project.optional-dependencies] dev` list has no
  `pytest-randomly` entry (checked directly — only `pytest>=8`, `ruff`,
  `pre-commit`, `grimp`, `import-linter`, `build`).
- A fresh `pip install -e ".[dev]"` does **not** install it —
  `pip show pytest-randomly` → "Package(s) not found".
- Passing `--randomly-seed=1` to pytest **errors outright**:
  `unrecognized arguments: --randomly-seed=1` — proof the plugin isn't loaded
  (pytest doesn't silently ignore an unknown `--randomly-*` flag from a
  registered plugin; this is argparse rejecting a genuinely-unregistered flag).
- pytest's default (no order-randomizing plugin) collection order is
  deterministic — same file traversal order every run, every OS.

**Conclusion: the 3 failures are not seed/order-flaky. They are deterministic
given the current file order, and only manifest under specific environment
conditions (see below) — not under CI's actual conditions.**

## 2. Real CI history check (`gh run list` / `gh run view`)

Last 15 runs on `main` and other branches: mostly `success`. The one recent
`failure` (run `31669565908`, 2026-08-13 05:12:55Z, windows/macos/ubuntu all
red) was a **different, already-fixed** issue — `test_version_sync.py`
(`1.0.54` != `1.0.56`), `test_lead_context_compact.py` (SOLO-mode dead
assertion), `test_done_note_symmetrize.py` (evidence-suffix assertion) —
exactly the 3 failures fixed by commit `1ca2ec2` ("fix: CI แดง 3 test fail
(windows-latest)"). Two green runs followed on `main` afterward. **No CI run
in the visible history shows the `data-eng`/`pane_env` failures this task was
about** — those never actually broke a real CI job.

## 3. Root cause of the 3 failures (reproduced locally, deterministically)

Ran `pytest -q tests/` on this machine with the fresh venv — same 3 failures
as the prior gate report, every time, no seed involved:

### 3.1 `test_pane_tools_policy.py::TestKnownRoles::test_includes_registered_custom_role`
### 3.2 `test_roles.py::TestByName::test_unknown_role_returns_none`

Both caused by the **same** leak: `roles._CUSTOM` is a process-global
in-memory dict (`src/agent_takkub/roles.py:93`). Every test file that
registers a custom role through it isolates properly (monkeypatches
`custom_roles.CUSTOM_ROLES_FILE`/`CUSTOM_AGENTS_DIR` to `tmp_path` and
clears/restores `roles._CUSTOM` around the test) — **except**
`tests/test_headless_entrypoint.py`. Its two tests call
`headless_mod.main([])`, which unconditionally runs
`custom_roles.load_and_register_all()` as part of boot
(`src/agent_takkub/headless.py:67`). Neither test isolates
`custom_roles.CUSTOM_ROLES_FILE` / `CUSTOM_AGENTS_DIR`, so that call reads
the **real, unpatched** settings directory — on this dev machine,
`C:\Users\monch\.takkub\custom-roles.json` and
`C:\Users\monch\.takkub\agents\data-eng.md`, both of which had a genuine
leftover `data-eng` entry (label `"Data-eng"`, color `#94a3b8`, row `99` —
exactly `custom_roles.py`'s orphan-doc self-heal fallback signature,
apparently a stray artifact from bug #162 investigation work on this
machine, not something this task should delete without asking). This gets
registered into `roles._CUSTOM` for real, with **no teardown** — every test
running afterward in the same pytest process that expects `"data-eng"`
unregistered fails.

Confirmed by testing `TestKnownRoles`/`TestByName` alone (pass) vs. after
`test_headless_entrypoint.py` runs first in the same process (fail) —
this is pure test-isolation debt, unrelated to any RNG seed. On a **fresh**
CI runner (no pre-existing `~/.takkub`), `load_and_register_all()` finds
nothing and registers nothing — which is exactly why CI never observed this.
It's a latent bug that only bites a **local dev's own machine** once its real
`~/.takkub` has any leftover custom-role file — it happened to bite this
specific box because of exactly that.

**Fix**: added an `autouse` fixture to `tests/test_headless_entrypoint.py`
that isolates `custom_roles.CUSTOM_ROLES_FILE`/`CUSTOM_AGENTS_DIR` to
`tmp_path` and clears/restores `roles._CUSTOM`, matching the pattern already
used by `test_settings_window.py`, `test_settings_management_ui.py`,
`test_settings_management_roles.py`, `test_custom_roles.py`, and
`test_role_registry_sync.py`. Did **not** touch the real leftover
`~/.takkub/custom-roles.json`/`agents/data-eng.md` on this machine — that's
real local state outside the repo, not something to delete without asking.

### 3.3 `test_orchestrator_env_allowlist.py::test_build_pane_env_includes_path`

Asserted `_build_pane_env()`'s `PATH` equals the monkeypatched value exactly.
`pane_env.py`'s `_apply_win32_path_sanitization()` (commit `4dc974a`, Bug
#156 fix) **intentionally** prepends `%APPDATA%\npm` to `PATH` on win32 when
that directory exists on the machine running the tests — confirmed by
reading the function: real, deliberate behavior, not a defect. The test's
exact-equality assertion never accounted for that, so it fails on any
Windows machine that actually has `%APPDATA%\npm` (true here; a stock CI
`windows-latest` runner without any prior `npm install -g` typically
wouldn't have it, which is also why CI never saw this one).

**Fix**: the test now `monkeypatch.delenv("APPDATA")` before setting `PATH`,
so `_apply_win32_path_sanitization`'s `os.path.isdir(npm_dir)` check is
deterministically `False` regardless of what's actually installed on the
machine — assertion holds on every machine. Did not touch the (correct,
intentional) production sanitizer.

## 4. Proof of fix — repeated full-suite runs

```
./.venv-qa/Scripts/python.exe -m pytest -q tests/test_orchestrator_env_allowlist.py \
    tests/test_pane_tools_policy.py tests/test_roles.py tests/test_headless_entrypoint.py
```
→ all green (96 tests, isolated files together — reproduces the same process
adjacency that used to fail).

Full suite (`pytest -q tests/`) run to completion after the fix: **green**,
no exit-127/abort. (pytest-randomly not being a real plugin here, "5 seeds"
as literally requested in the task isn't meaningful — order is deterministic
either way. Ran the full suite twice back-to-back instead to confirm
determinism across repeats, both green.)

`ruff check src/ tests/` → All checks passed.
`lint-imports` → Contracts: 23 kept, 0 broken.

## 5. Should CI pin a random-order seed?

**Not applicable as literally asked** — there is no order-randomization
plugin in this repo's dependency graph today, so there's no seed to pin.

If the team is separately considering **adding** `pytest-randomly` to CI
(a legitimate idea — order-dependent test bugs like the one just fixed are
real and this investigation only found it by accident, via a corrupted local
`~/.takkub`, not by design):

- **Pro of fixed seed**: results stay reproducible/bisectable; a red CI run
  can be reproduced locally with the same `--randomly-seed`.
- **Con of fixed seed**: it locks in whatever pollution happens to not
  collide at that one seed — new order-dependent bugs introduced later can
  sit invisible indefinitely, same failure mode this task just found by luck.
- **Recommendation**: if adopted, do NOT pin — let CI pick a fresh seed every
  run (default `pytest-randomly` behavior) and print it in the log (it does,
  by default) so a red run is reproducible after the fact via that printed
  seed, without permanently blinding the suite to future pollution the way a
  pinned seed would. This is a separate opt-in decision, not something this
  task should silently add — flagging back to Lead/user rather than assuming.

## 6. Files changed

- `tests/test_headless_entrypoint.py` — isolate `custom_roles` registry
  paths + `roles._CUSTOM` around both boot tests (autouse fixture).
- `tests/test_orchestrator_env_allowlist.py` — `test_build_pane_env_includes_path`
  now deterministic regardless of the machine's real `%APPDATA%\npm` state.

No production code changed — both bugs were test-isolation gaps, not app bugs.
