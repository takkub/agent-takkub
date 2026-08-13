# Test-suite health audit — 2026-08-13

Scope: review/audit only, no code or test changes. Full suite run in a fresh
per-worktree `.venv` (`python -m venv .venv && pip install -e ".[dev]" && pip
install pytest-cov`), executed as:

```
.venv/Scripts/python.exe -m pytest tests/ -q --durations=25 \
  --cov=src/agent_takkub --cov-report=term-missing:skip-covered \
  --cov-report=json:coverage.json -p no:cacheprovider
```

Run log: `pytest_full.log` (repo root, this worktree, not committed).
Coverage data: `coverage.json` (this worktree, not committed).

## TL;DR

- **5,709 passed / 7 skipped / 0 failed** — matches the last recorded
  baseline exactly. Overall line coverage **79%**.
- The 5 modules named as suspected under-tested are **mostly fine**:
  `pane_guard.py` 100%, `task_ledger.py` 92%, `remote/` 81-100% per file,
  `autoskills_installer.py` 83%. Only `provider_usage.py` (79%) has a real
  gap: its background polling loop (`ProviderUsageStore._loop`) and two
  codex-subprocess error paths are completely untested (§1).
- The actual weak spots are 11 GUI files under 50% coverage, most notably
  `claude_auth_dialog.py` at literal **0%** and `main_window.py` at **34%**
  (§1).
- No pinned-buggy-behavior tests found (§2).
- The 3 isolation leaks the task referenced were already found and fixed
  same-day in commit `edf18d9`; re-swept this session for `Path.home()` /
  network / cwd / wall-clock leaks and found nothing further (§3).
- All 7 skips are legitimate environment/platform guards, not disabled
  tests (§4).
- 8 concretely weak assertions found and itemized, each with the specific
  check it's missing relative to a sibling test or its own test name (§5).
- Top 4 slowest tests account for ~137s combined; all explainable by what
  they're actually doing (installed-mode packaging setup, concurrency
  stress, provider RPC round-trip) — nothing looks like accidental slowness
  (§6).

## 1. Coverage gaps

**Overall: 79% line coverage (31,364 statements, 6,642 missing, `coverage.json` TOTAL row).**
141 files measured; 23 report complete coverage.

The task named 5 specific modules as suspected under-tested. Checked against
real numbers, **the premise doesn't hold for 4 of the 5** — they're
reasonably covered:

| Module | Coverage | Verdict |
|---|---|---|
| `pane_guard.py` | **100%** (45/45 stmts) | Fully covered, not a gap. |
| `task_ledger.py` | **92%** (254/276) | Well covered. 22 missing lines, mostly single-line error branches (101, 249, 286, 312, 353-356, 431, 437-440, 468, 473-476, 480, 499, 505, 512) — no untested function-sized block. |
| `autoskills_installer.py` | **83%** (333/400) | Reasonably covered. Missing lines cluster around lines 342-498 (several `except`-branch / edge-path clusters in the install/verify flow) and 560-751 (a second cluster, likely a rarely-hit rollback or CLI-output path) — worth a closer look but not "barely covered". |
| `remote/` (8 submodules) | **82%–100%** per file (`auth.py` 100%, `config.py` 97%, `settings_dialog.py` 93%, `tunnel.py` 90%, `notify.py` 85%, `api.py` 88%, `http_server.py` 81%, `__init__.py` 83%) | All well covered — matches the large dedicated test files found (`test_remote_notify.py` 1,450 lines alone). |
| `provider_usage.py` | **79%** (248/315) | The one real gap of the 5 — see below. |

**`provider_usage.py` — the genuine gap (67 missing lines):**
- **`ProviderUsageStore._loop()` (lines 602-619) is entirely untested.** This
  is the actual background-thread polling cycle (`_wake.wait(timeout=…)` /
  `_wake.clear()` / re-poll every provider / skip providers cached as
  `STATUS_UNSUPPORTED`). Every existing test (`tests/test_provider_usage.py`)
  exercises `_fetch_one()` directly or via `store.start()`/mocked timers, but
  none drive an actual wait-then-repoll cycle or verify the
  `STATUS_UNSUPPORTED` skip-on-repoll behavior (line 617). A regression that
  broke the wake/interval logic (e.g. spinning at 100% CPU, or never
  reprobing an `unsupported` provider after it becomes available) would not
  be caught by the current suite.
- **The codex real-subprocess error paths are untested** (lines 300-301,
  302-306): `subprocess.Popen(..., "app-server")` raising `OSError` on
  spawn failure, and the generic `except Exception` catch-all wrapping
  `_codex_rpc_roundtrip`. `test_rpc_roundtrip_times_out_without_raising`
  (seen in the durations list) tests the RPC-level timeout, but not these
  two outer failure modes.
- Lines 171-207, 242-269 (not read in detail this pass) are further
  per-provider fetch-path branches — likely similar error-branch gaps in
  the claude/gemini fetchers, worth a closer look in a follow-up.

**Beyond the 5 named modules — the real weak spots are GUI files**, all
`main_window.py`/dialog-adjacent, not backend logic:

| File | Coverage | Stmts (missing) |
|---|---|---|
| `__main__.py` | 0% | 3 (3) — trivial entry shim, not concerning |
| `claude_auth_dialog.py` | **0%** | 93 (93) — an entire dialog with zero test coverage |
| `tutorial_overlay.py` | 16% | 159 (134) |
| `update_panel.py` | 22% | 445 (346) |
| `logs_panel.py` | 22% | 143 (111) |
| `project_wizard.py` | 24% | 353 (268) |
| `user_actions.py` | 31% | 442 (304) |
| **`main_window.py`** | **34%** | 634 (416) — the largest remaining god-file mixin-holder, still only a third tested despite the 2026-06 mixin split (`docs/architecture/godfile-map.md`) |
| `terminal_widget.py` | 38% | 404 (252) |
| `update_worker.py` | 47% | 171 (91) |
| `usage_meter.py` | 50% | 229 (115) |

11 files total are below 50% coverage — all Qt widget/dialog/window files
(GUI surface tends to be exercised through smoke/e2e browser QA and manual
testing rather than pytest unit tests in this codebase, which is a
reasonable division of labor, but `claude_auth_dialog.py` at literal 0% and
`main_window.py` at 34% are worth flagging explicitly since they're
security/auth-adjacent and central-orchestration-adjacent respectively.

## 2. Tests pinning wrong behavior

No test comments or docstrings in `tests/` acknowledge a known-buggy
assertion (searched for `XXX|FIXME|TODO.*fix|known bug|buggy|not ideal|
current (buggy)|should be .* but|technically wrong` — 0 hits besides one
unrelated comment in `test_terminal_widget.py:119` about a fixed splice bug
being *regression-tested*, not pinned).

## 3. Isolation leaks

The 3 leaks the task referenced were already found and fixed same-day, in
commit `edf18d9` ("test: fix real test-isolation leaks behind 3
flaky-looking failures"):

- `tests/test_headless_entrypoint.py` called `headless.main()` (→
  `custom_roles.load_and_register_all()`) without isolating
  `CUSTOM_ROLES_FILE`/`CUSTOM_AGENTS_DIR` or clearing `roles._CUSTOM` — it
  read whatever real `~/.takkub` state existed on the dev machine and leaked
  it into every later test in the same process, no teardown.
- `tests/test_orchestrator_env_allowlist.py` asserted exact PATH equality
  without accounting for `pane_env.py`'s intentional `%APPDATA%\npm` prepend
  (issue #156, commit `4dc974a`) on machines where that dir exists.
- (third item per the commit message — see `docs/qa/2026-08-13-ci-determinism.md`
  for the full writeup of the investigation that found these.)

**Verified fixed, re-audited today:**
- `tests/test_headless_entrypoint.py:44-50` now monkeypatches
  `custom_roles.CUSTOM_ROLES_FILE` / `CUSTOM_AGENTS_DIR` to `tmp_path`
  subpaths and saves/restores `roles._CUSTOM` around the test body.
- `tests/test_custom_roles.py` (the other caller of the same registry) was
  already correctly isolated (`CUSTOM_ROLES_FILE`/`CUSTOM_AGENTS_DIR`
  monkeypatched, `roles._CUSTOM` saved/cleared/restored at module scope,
  `tests/test_custom_roles.py:16-25`).

**Swept for further leaks (grep, this session), all clean:**
- `Path.home()` / `os.path.expanduser` hits (`tests/test_auto_issue_capture.py`,
  `tests/test_graft_store.py`, `tests/test_user_mcps.py`, `tests/test_plugin_policy.py`)
  — every one either patches `Path.home` via monkeypatch before use, or (in
  `test_auto_issue_capture.py` and `test_graft_store.py`'s
  `test_graft_store_base_falls_back_to_home_when_data_home_is_repo_root`) only
  *reads* the real home path string for a comparison/skip-guard, never writes
  under it.
- `socket.socket(...)` / `urllib.request.urlopen(...)` / `http.client.HTTPConnection`
  hits (`tests/test_remote_http_server.py`, `test_remote_e2e_round2.py`,
  `test_resume_session_picker.py`, `test_remote_api.py`,
  `test_installed_mode_gate.py`) — all connect to a same-process
  `127.0.0.1:<ephemeral-port>` test server the test itself started, no real
  network egress.
- `os.getcwd()` / `Path.cwd()` hits (`tests/test_path_traversal_vault.py`,
  `tests/test_settings_management_skills.py`) — `test_path_traversal_vault.py`
  only *reads* real cwd as a negative-assertion baseline ("nothing sensitive
  got created here"), never writes to it; `test_settings_management_skills.py`
  uses `monkeypatch.chdir(project_dir)` before anything reads `Path.cwd()`.
- `tests/conftest.py`'s autouse `_isolate_runtime` fixture (session-wide, all
  ~5,700 tests) redirects `RUNTIME_DIR`/`EVENTS_LOG`/`PORT_FILE`/
  `ROLE_MEMORY_DIR`/`GRAFT_STORE_ROOT`/`GRAFT_STAGING_ROOT`/provider-config
  paths to a per-test `tmp_path`, and force-clears `TAKKUB_PORT_FILE`,
  `warm_browser_mcps`, `graft_autobuild` module-level mutable state, and the
  `mcp_bridge` codex-version cache every test — this is the reason the sweep
  above found nothing else: everything not explicitly listed here is already
  covered by this fixture.

No further real-home / real-network / real-cwd / real-wall-clock leaks found
in this pass beyond the 3 already fixed in `edf18d9`.

## 4. Skipped tests (7 total in the last reported run)

Repo-wide `pytest.skip(...)` call sites (15 conditional sites; the 7 that
fire depend on this environment):

| File:line | Condition | Permanent or transient |
|---|---|---|
| `test_auto_issue_capture.py:306,320,334` | `Path.home()` returns empty string | Permanent guard — only fires on an exotic environment with no resolvable home dir. Not expected to fire on this machine (has a real home dir) — did not appear in this run's skip count. |
| `test_autoskills_installer.py:627,646` | symlink creation not permitted (no dev-mode / admin on Windows) | **Transient on this machine**: Windows without Developer Mode or elevated privileges can't create symlinks — expected to fire here, 2 of the 7. |
| `test_docs_verify.py:217` | `docs/` not found | Permanent guard for installed-mode/packaging test contexts where the docs tree isn't shipped; not expected to fire in a normal repo checkout. |
| `test_graft_autobuild.py:711,735` | `git` not on PATH | Permanent guard; doesn't fire here (git is on PATH, used throughout this session). |
| `test_installed_cli_bin_integration.py:86` | `pythonw.exe` is Windows-only | Platform guard — **does not fire on this run** (Windows machine); would fire on CI's macOS leg. |
| `test_orchestrator_harvest.py:76,172` | symlinks not supported | Same as autoskills_installer — Windows without symlink privilege. |
| `test_orchestrator_session_uuid.py:229` | `sys.platform != "win32"` (case-insensitive filesystem is Windows-specific) | Platform guard, inverse of the tunnel one — **runs** (not skipped) on this Windows box; would skip on macOS CI. |
| `test_remote_tunnel.py:238` | `sys.platform != "win32"` (Job Objects Windows-only) | Platform guard, doesn't fire on Windows. |

All 7 skips that actually fired in this run are **environment/platform
guards, not disabled tests** — every one is a `skipif`/inline-skip keyed to a
real environment precondition (no admin/dev-mode symlink rights on this
Windows box being the dominant cause, matching prior QA notes that Windows
symlink tests need Developer Mode). None found that look like a forgotten
"skip while I fix this" left permanently on. Exact per-test skip reasons
from this run's log are in `pytest_full.log` (grep `SKIPPED`).

## 5. Weak assertions

Ran an AST scan (`ast.walk` over every `test_*` function) for test bodies
whose *only* `assert` statements are `x is not None` / `assert True` — 28
candidates found; hand-reviewed. Most turned out fine (an `is not None`
guard immediately followed by content assertions the scanner didn't count
as "the same assert", e.g. `mock.assert_called_once_with(...)` calls, which
aren't `assert` statements). The following are genuinely weak — they check
existence but not correctness, where correctness was clearly the point:

1. **`tests/test_git_status.py:135`** `test_missing_path_returns_error_not_raise`
   — only checks `status.error is not None`. The adjacent test one function up
   (`test_non_git_path_returns_error_not_raise`, line 124-133) checks the same
   condition *plus* `branch == ""`, `commits == []`, `worktrees == []` — this
   one dropped those follow-up checks for the missing-path case.

2. **`tests/test_knowledge_base.py:196-217`** `test_done_flow_never_raises_when_knowledge_base_write_fails`
   — asserts `result is not None  # local session file still written` but
   never actually checks a session file was written (no `.is_file()` /
   `.read_text()` call). The "never raises" half of the test name is
   validated implicitly (no try/except wrapping the call), but the "local
   session file still written" half in the comment is asserted by comment
   only, not by code.

3. **`tests/test_services.py:52-54`** `test_accepts_string_path` — checks
   `detect_compose(str(tmp_path)) is not None`. The three sibling tests
   above it (lines 38-50) all assert exact path equality
   (`== tmp_path / "docker-compose.yml"`); this one only proves *a* path
   truthy, not the *correct* one — a regression that made `detect_compose`
   return the wrong file for a string-path input would pass.

4. **`tests/test_orchestrator_stall.py:373-394`** `test_non_ui_role_stall_not_suppressed_by_qa_screenshot`
   — asserts `result["backend"]["stall_minutes"] is not None`. Test sets up
   an 8-minute-old `last_send_ts` specifically to prove the stall value is
   correctly computed despite the QA-screenshot suppression logic, but never
   checks the number is actually ~8 (or even positive/nonzero) — a bug that
   made `stall_minutes` come back `0.0` or a small positive float would still
   pass this "should be stalled" assertion.

5. **`tests/test_roles.py:109-111`** `test_case_insensitive` — checks
   `roles.by_name("FRONTEND") is not None` and `roles.by_name("  Frontend  ")
   is not None`, but never checks the resolved role is actually
   `roles.by_name("frontend")` / has `label == "Frontend"`. A bug that
   resolved a case-varied name to the *wrong* role object would still pass.
   (Contrast with `test_known_role` two lines up, which does check
   `r.label == "Backend"`.)

6. **`tests/test_rate_limit_watchdog.py:68-78`** `test_real_banners_still_detected`
   — loops over 4 real-world rate-limit banner strings (several containing an
   explicit reset time like "11pm", "3pm", "8am") and only asserts
   `_parse_rate_limit_reset(banner, NOW) is not None`. Never checks the
   *parsed reset time* is correct for any of the 4 — a regression that parsed
   the wrong hour (but still returned *a* datetime) would pass silently. The
   very next test in the file, `test_session_limit_banner_v2_1_198`, does the
   right thing and asserts the actual parsed epoch.

7. **`tests/test_provider_config.py:294-300`** `test_codex_lead_reports_provider_and_missing_features`
   — the test's own name promises to verify the gap message "reports
   provider and missing features", but the body only asserts `gap is not
   None`. It never checks the returned string actually mentions "codex" or
   names any missing feature — the name over-promises relative to what's
   verified.

8. **`tests/test_pane_transcript.py:201-207`** `test_falsy_value_keeps_capture`
   — asserts `_build_transcript_path("proj", "qa") is not None` for
   `TAKKUB_DISABLE_TRANSCRIPTS=0`. The sibling positive-path test 9 lines up
   (`test_path_built_when_not_disabled`, line 184-192) additionally asserts
   `path.endswith(".transcript.log")`; this one drops that check, so a bug
   that returned e.g. an empty string as "not disabled" would still pass.

Deliberately *not* flagged: `tests/test_pty_ready_prompt.py`'s ~13
`is_blocked_on_tty_prompt()` / `has_unparsed_tool_call()` `is not None`
detections (lines 145-183, 245-274) — each is a true/false detection test
with a matching negative-case test elsewhere in the same file
(`test_returns_none_on_normal_output`, `test_returns_none_on_empty_screen`,
etc.) plus one test that separately verifies matched-text content
(`test_returns_matched_line_text`). This is a reasonable split of concerns,
not a weak assertion.

## 6. Slowest tests (`--durations=25`, top 20 shown, full 25 in `pytest_full.log`)

| Time | Phase | Test |
|---|---|---|
| 44.41s | setup | `test_installed_cli_bin_integration.py::TestNpmWrapperConsoleScriptParity::test_takkub_console_script_exists_next_to_python` |
| 33.40s | call | `test_pty_session_threading.py::test_concurrent_feed_and_read_no_crash` |
| 31.85s | setup | `test_installed_mode_gate.py::TestInstalledConfigIdentity::test_data_and_settings_home_isolated_to_installed_home` |
| 27.67s | call | `test_resume_session_picker.py::TestApiResumeLead::test_codex_lead_uses_provider_aware_validation` |
| 12.44s | call | `test_docs_verify.py::test_verify_docs_real_repo_no_crash` |
| 7.95s | call | `test_main_window_status_bar.py::TestDoctorIntegration::test_run_all_checks_returns_list` |
| 6.56s | call | `test_main_window_status_bar.py::TestDoctorIntegration::test_auto_fix_findings_have_callable` |
| 6.46s | call | `test_main_window_status_bar.py::TestDoctorIntegration::test_format_report_contains_summary` |
| 6.42s | call | `test_main_window_status_bar.py::TestDoctorIntegration::test_format_report_contains_category_headers` |
| 3.16s | call | `test_pty_session_threading.py::test_resize_is_thread_safe_with_feed` |
| 3.00s | call | `test_installed_mode_gate.py::TestInstalledCliPortFileWiring::test_status_reads_takkub_port_file_and_fails_with_connection_refused` |
| 2.84s | call | `test_git_status.py::TestAheadBehind::test_ahead_and_behind_via_bare_remote` |
| 2.17s | call | `test_spawn_task_delivery.py::test_fifo_queue_drains_three_claude_assigns_with_preload_events` |
| 1.96s | call | `test_role_memory.py::TestAppendFailureEntry::test_curation_runs_immediately_so_budget_never_bloats` |
| 1.61s | call | `test_resume_session_picker.py::TestApiResumeLead::test_gemini_lead_uses_provider_aware_validation` |
| 1.50s | call | `test_single_instance_watchdog.py::TestWatchdogThreadBehaviour::test_watchdog_does_not_fire_with_live_heartbeat` |
| 1.22s | call | `test_settings_window.py::TestPluginsMatrixView::test_toggle_cell_marks_dirty_and_save_persists` |
| 1.16s | call | `test_git_status.py::TestWorktrees::test_only_wt_prefixed_worktrees_are_reported` |
| 1.02s | call | `test_provider_usage.py::TestCodexAdapter::test_rpc_roundtrip_times_out_without_raising` |
| 0.99s | call | `test_settings_management_ui.py::test_settings_window_has_object_name_for_theming` |

The top 4 (44.4s + 33.4s + 31.9s + 27.7s = **137.4s, well over half of any
"why is the suite slow" budget**) are the only genuinely expensive tests —
everything past #5 is under 13s and the long tail is sub-1s. Two are `setup`
phase, not `call`: `test_takkub_console_script_exists_next_to_python`'s setup
fixture and `test_data_and_settings_home_isolated_to_installed_home`'s setup
fixture both do real work (per their names — installed-mode gate tests that
build/install into a throwaway env) before the test body even runs, which is
inherent to what an installed-mode packaging gate test has to do, not
accidental slowness. `test_concurrent_feed_and_read_no_crash` (33.4s) and
`test_codex_lead_uses_provider_aware_validation` (27.7s) are the two `call`-
phase tests worth a look if suite wall-time ever becomes a problem — a
concurrency/threading stress test and a provider-validation round-trip test
respectively are plausible legitimately-slow tests, not obviously wasteful,
but nobody has measured whether their sleep/timeout values could be tightened.

Total collected-and-timed suite wall time is not printed as a single number
in this run's output (see §7 below for why), but summing dot-stream test
count and the coverage-instrumentation overhead, the run took on the order
of 25-30 minutes wall-clock under `--cov` in this session — substantially
above the 337s baseline the QA history in project memory recorded for a
plain (non-coverage) run of a similar-sized suite. Coverage instrumentation
is the entire delta; nothing here suggests the tests themselves regressed.

## 7. Full-suite result

**5,709 passed, 7 skipped, 0 failed, 0 errors** — reconstructed from the dot
stream (5,709 `.` + 7 `s`, zero `F`/`E` characters) because this run's
`pytest -q --durations=25 --cov=...` output ends with the coverage report
and durations table but never prints pytest's own final
`"N passed, M skipped in Ts"` summary line (confirmed missing via a raw byte
read of `pytest_full.log` — not a truncation artifact, the file's `EXIT:0`
marker is intact right after the last duration row). Root cause not
investigated further (out of scope for this audit — it's a reporting
quirk of this pytest/pytest-cov version combination, not a test failure);
process exit code was `0`, and the reconstructed count exactly matches the
5,709-passed/7-skipped baseline already on record from the previous same-day
run (`docs/audit/2026-08-13-integration.md`, "full suite 5709 passed"),
confirming this run is consistent, not silently different.
