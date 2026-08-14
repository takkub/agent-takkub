# #238 + #239: subprocess-guard alias blindness, CI main red

## #238 — subprocess encoding/no-window guards blind to aliased imports

### Root cause

`tests/test_subprocess_text_encoding_guard.py` and
`tests/test_subprocess_no_window_guard.py` each had their own copy of
`_is_subprocess_call()`, which only matched a call shaped like
`subprocess.run(...)` — literally requiring the base name `subprocess`:

```python
if isinstance(func.value, ast.Name) and func.value.id == "subprocess":
```

`src/agent_takkub/update_panel.py` writes `import subprocess as _subprocess`
(to avoid clashing with a local `import subprocess` inside the same
function) and calls `_subprocess.run(...)`. That call shape never matches
`func.value.id == "subprocess"`, so both guards silently ignored it —
`_find_violations()` returned `0` for a file that actually had two
text-mode calls missing `encoding=`/`errors=`.

### Fix

- Added `tests/_subprocess_call_ast.py`: a shared helper
  (`collect_subprocess_aliases()` + `is_subprocess_call()`) that resolves,
  per-file, every name bound to the `subprocess` module (`import subprocess
  as X`) and every function alias (`from subprocess import run as _run`)
  from that file's own AST, instead of hardcoding the literal name
  `"subprocess"`. Both guard test files now import and use it, eliminating
  the duplicated (and duplicately-buggy) detection logic.
- Fixed the two real violations in `src/agent_takkub/update_panel.py` that
  the alias-aware guard now catches:
  - `_find_global_postinstall()` (`_subprocess.run([npm, "root", "-g"], ...)`)
  - `_NpmUpdateThread.run()` (`_subprocess.run([node, str(postinstall_js)], ...)`)

  both now pass `encoding="utf-8", errors="replace"` — same crash class as
  #205 (Thai-locale `cp874` byte the OS codepage can't decode kills the
  reader thread), and this code path is the cockpit's own auto-updater, so
  it runs unattended on every user's machine.
- Added negative tests proving the alias resolution actually works, in both
  guard files (`test_guard_catches_aliased_import_missing_encoding`,
  `test_guard_catches_aliased_function_import_missing_encoding`, and the
  `..._creationflags` equivalents in the no-window guard) — each writes an
  offending file to `tmp_path` using an aliased import and asserts
  `_find_violations()` reports exactly one violation.

### Proof the alias hole is real (before fix)

Before wiring in the shared alias-aware helper, running the new negative
test against the *old* `_is_subprocess_call()` (literal `== "subprocess"`
check) on this offender:

```python
import subprocess as _subprocess

def f():
    return _subprocess.run(['echo'], capture_output=True, text=True)
```

produced `violations == []` — zero findings on a call that is missing both
`encoding=` and `errors=`.

### Proof it's caught now (after fix)

Same offender file, run through `_find_violations()` with the alias-aware
helper:

```
...\offender.py:4: subprocess.run() opens text mode but is missing encoding, errors= (needs encoding="utf-8", errors="replace")
```

`len(violations) == 1` — confirmed via
`test_guard_catches_aliased_import_missing_encoding` /
`test_guard_catches_aliased_function_import_missing_encoding` (encoding
guard) and the `..._creationflags` pair (no-window guard), all passing.

Re-running both parametrized guard suites against the current
`src/agent_takkub/` tree (now alias-aware) found no other violations beyond
the two already fixed in `update_panel.py`.

## #239 — CI main red on all 3 OS

### `test_performance_stress_harness.py::test_deterministic_stress_harness_a_through_i`

Hardcoded `root / ".venv" / "Scripts" / "python.exe"` — a Windows-shaped
path that assumes a `.venv` exists inside the checkout. CI runners install
dependencies into the runner's own interpreter, not a repo-local `.venv`,
so this path never exists on any of the 3 CI OSes. Fixed by using
`sys.executable` (the interpreter already running the test) instead.

### `test_job_object_manager.py::test_windows_job_assigns_process_and_closes_kill_on_close_handle`

Mocks `ctypes.windll.kernel32` and monkeypatches `module.sys.platform =
"win32"` at *runtime*, but `job_object_manager.py` defines its
`_JOBOBJECT_EXTENDED_LIMIT_INFORMATION` (and friends) `ctypes.Structure`
classes inside `if sys.platform == "win32":` at *import time*. On a
non-Windows CI runner those classes never exist, so `create()` raises
`NameError`, caught by its own broad `except Exception: return False`,
and `manager.assign(4242)` returns `False` instead of `True`. This is
fundamentally a Windows-only test (it can't retrofit types Python never
compiled), so it now carries
`@pytest.mark.skipif(sys.platform != "win32", ...)` — the sibling
`test_non_windows_job_manager_is_safe_noop` in the same file already
covered the non-Windows no-op path and needed no change.

### Sweep for the same pattern elsewhere in `tests/`

Grepped `tests/` for `.venv/Scripts`, `Scripts/python`, hardcoded `C:\`
paths, and every `subprocess.run/Popen/...` call site. Findings:

- `test_worktree_manager.py`, `test_installed_mode_gate.py`: reference
  `.venv/Scripts/python.exe` only as an expected/synthetic path inside a
  `tmp_path`-scoped fake venv, or behind an explicit `sys.platform ==
  "win32"` branch with a POSIX fallback — not a host-environment assumption.
- `test_autoskills_installer.py`, `test_image_input.py`,
  `test_mcp_bridge.py`, `test_orchestrator_env_allowlist.py`,
  `test_pip_sync_script.py`: hardcoded `C:\...` strings are inputs to pure
  string/path-normalization functions (mocked or `is_windows=`-parameterized)
  — no dependency on the actual host OS.
- `test_remote_tunnel.py`: the one real subprocess call using a
  Windows-only `ping -t` flag is already behind
  `@pytest.mark.skipif(sys.platform != "win32", ...)`.
- `test_provider_usage.py`, `test_installed_cli_bin_integration.py`,
  `test_skill_scan.py`, `test_graft_autobuild.py`, `test_git_status.py`:
  subprocess calls use `sys.executable` or PATH-resolved `git`, no hardcoded
  interpreter path.

No other instance of the same bug class found.

## Verification

Targeted runs only (full suite is QA's batch-gate job per project policy),
via the shared dev venv with `PYTHONPATH` pointed at this worktree's `src/`
(shared venv's editable install points at the base checkout — see #202;
never `pip install -e .` from a worktree):

```
tests/test_subprocess_text_encoding_guard.py   — 92 passed
tests/test_subprocess_no_window_guard.py       — 94 passed
tests/test_npm_update_thread.py                — 18 passed
tests/test_performance_stress_harness.py       — 1 passed
tests/test_job_object_manager.py               — 2 passed (Windows; skipif
                                                   confirmed correct — the
                                                   win32-only test runs here
                                                   because this box IS win32)
```

`ruff check` and `ruff format --check` both clean on every changed file.
