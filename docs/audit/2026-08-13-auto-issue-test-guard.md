# #188 — auto_issue_capture fired a real GitHub issue from a pytest process

## Root cause

`app.py` calls `_install_exception_guard()` at **module import time**
(`app.py:120`), which installs a non-default `sys.excepthook` /
`threading.excepthook` / `sys.unraisablehook`. Any process that imports
`agent_takkub.app` — including a pytest run that only imports it
transitively (`tests/test_app_remote_teardown.py` does this directly, and
other modules pull it in through the import graph) — gets its own unhandled
exceptions routed through `_log_unhandled` (`app.py:57`) →
`capture_cockpit_crash` (`auto_issue_capture.py`), which files a real issue
against the **public** `takkub/agent-takkub` repo.

`capture_cockpit_crash` already had a 24h dedup + 5/24h rate cap, but no
guard at all for "this isn't a cockpit process". Confirmed via
`runtime/boot.log:421-430`: pid=32640, `UNHANDLED EXCEPTION
(sys.excepthook)`, traceback rooted in `pytest.console_main() ->
sys.stdout.flush() -> OSError [Errno 22]` — a background pytest process's
stdout pipe broke when it was killed, and the resulting `OSError` sailed
straight into the installed hook. `auto_issue_dedup.json`'s
`signatures.OSError` entry confirms the issue was actually filed.

## Fix

`src/agent_takkub/auto_issue_capture.py`: added `_auto_issue_suppressed()`
as a single seam checked at the top of `capture_cockpit_crash()`. Returns
`True` (no-op) when any of:

- `TAKKUB_SKIP_AUTO_ISSUE_CAPTURE` env var is set (new member of the
  `TAKKUB_SKIP_*` guard family alongside `TAKKUB_SKIP_MCP_WARM` /
  `TAKKUB_SKIP_GRAFT_BUILD` / `TAKKUB_SKIP_ORPHAN_WORKTREE_PRUNE` /
  `TAKKUB_SKIP_NATIVE_CHROME` in `tests/conftest.py`)
- `PYTEST_CURRENT_TEST` is set (pytest sets this only *during* an individual
  test — narrowest signal, but doesn't cover the actual #188 shutdown-path
  crash since that happened outside any single test)
- `CI` env var is set
- `"pytest" in sys.modules` — the catch-all: stays true for the entire
  process lifetime once pytest has been imported, which is what actually
  covers the #188 repro (a broken-pipe `OSError` during `pytest.console_main`
  shutdown, not inside a running test)

`tests/conftest.py`: `TAKKUB_SKIP_AUTO_ISSUE_CAPTURE=1` set both at
module-import time (`setdefault`, so it's on before the very first test
collects) and re-armed per-test in `_isolate_runtime` (belt-and-suspenders,
same pattern as its three siblings).

`tests/test_auto_issue_capture.py`: this module's entire point is
exercising `capture_cockpit_crash`'s real firing behaviour, so its autouse
`_isolate` fixture now also does
`monkeypatch.setattr(aic, "_auto_issue_suppressed", lambda: False)` —
overriding the guard back off at the one seam instead of every assertion in
the file going dark. No existing assertion was weakened; this only restores
the pre-#188 firing behaviour for tests that need it.

## New regression test

`tests/test_app_exception_guard.py` — imports `agent_takkub.app` for real,
re-installs the real `sys.excepthook` (`app_mod._install_exception_guard()`,
so it isn't accidentally exercising a stand-in left by another test), fires
a real `OSError` through it, and asserts `capture_cockpit_crash` neither
spawns a worker thread nor calls `issues.new_issue`.

**Proven not vacuous**: temporarily removed the
`if _auto_issue_suppressed(): return` guard from `capture_cockpit_crash`,
re-ran this test — it went red (`AssertionError: capture_cockpit_crash must
no-op ... assert [((), {'daemon': True, 'name': 'auto-issue-capture', ...})]
== []`), confirming the guard is what the test actually exercises. Guard
restored immediately after.

## Other call paths checked (task item 4)

`issues.new_issue` has exactly one other caller: `update_worker.py`'s
`ClaudeUpdateWorker._maybe_file_issue` (self-update analyzer — files an
issue when a Claude Code update needs manual review). That path is an
explicit, user/timer-triggered call, not wired to any process-level
exception hook, and isn't reachable by simply importing a module — out of
scope for #188's "any process that imports app.py" blast radius. Not
modified. `cli.py` has no `issues.*` / `capture_cockpit_crash` /
`excepthook` references at all.

## Verify

```
pytest tests/test_auto_issue_capture.py tests/test_app_exception_guard.py \
       tests/test_app_remote_teardown.py tests/test_mcp_warm_guard.py -q   # 33 passed
pytest tests/test_disk_usage.py tests/test_orphan_worktree_prune_guard.py -q  # 60 passed (prior round, still green)
ruff check <touched files>            # all checks passed
ruff format --check <touched files>   # 4 files already formatted
lint-imports                          # 24 kept, 0 broken
```

Full suite left to qa's batch gate per project convention.

## Fix-loop round 2 (2026-08-13): `""`/`"0"` convention drift

Lead review of the round-1 diff caught `_auto_issue_suppressed()`'s env
check using plain truthy (`if os.environ.get("TAKKUB_SKIP_AUTO_ISSUE_CAPTURE")`)
while its three `TAKKUB_SKIP_*` siblings —
`TAKKUB_SKIP_ORPHAN_WORKTREE_PRUNE` (`disk_usage.py`),
`TAKKUB_SKIP_GRAFT_BUILD` (`graft_autobuild.py`),
`TAKKUB_SKIP_MCP_WARM` (`shared_dev_tools.py`) — all use
`.strip() not in ("", "0")`. Under the old check, `TAKKUB_SKIP_AUTO_ISSUE_
CAPTURE=0` (a dev's intentional "turn this guard off") was still truthy as a
non-empty string, so it kept suppressing — the opposite of what `=0` means
for its three siblings.

**Fix**: `auto_issue_capture.py`'s env check now matches the shared
convention. `CI` was deliberately left plain-truthy (not converted) — it's
an external convention set by the CI provider (GitHub Actions sets it to the
literal string `"true"`), not one of our own `TAKKUB_SKIP_*` kill switches,
so a comment now documents why it's the odd one out instead of looking like
a missed case.

**New tests** (`tests/test_app_exception_guard.py`, not
`test_auto_issue_capture.py` — that file's autouse `_isolate` fixture stubs
`_auto_issue_suppressed` back to `lambda: False` for every test, so it can't
exercise the real function):
- `test_skip_env_zero_does_not_suppress` — env set to `"0"`, the other three
  suppression signals (`PYTEST_CURRENT_TEST`, `CI`, `"pytest" in
  sys.modules`) stripped via `monkeypatch.delenv`/`delitem`, asserts
  `_auto_issue_suppressed()` is `False`.
- `test_skip_env_nonzero_still_suppresses` — same isolation, env set to
  `"1"`, asserts `True` (companion coverage, not just the regression case).

**Proven not vacuous**: temporarily reverted the env check to the old plain-
truthy form, re-ran `test_skip_env_zero_does_not_suppress` alone — it went
red (`assert True is False`, i.e. `=0` was still suppressing). Fix restored
immediately after, full targeted set re-run green.

```
pytest tests/test_auto_issue_capture.py tests/test_app_exception_guard.py -q   # 28 passed
ruff check src/agent_takkub/auto_issue_capture.py tests/test_app_exception_guard.py tests/test_auto_issue_capture.py   # all checks passed
ruff format --check <same 3 files>   # already formatted
```

Only `auto_issue_capture.py` and `test_app_exception_guard.py` touched this
round (`test_auto_issue_capture.py` untouched — it was only run, not
edited) — per task scope, the
still-uncommitted round-1 files from other tasks (`disk_usage.py`,
`conftest.py`, `test_disk_usage.py`,
`test_orphan_worktree_prune_guard.py`) were left alone, and nothing was
`git add`ed or committed.
