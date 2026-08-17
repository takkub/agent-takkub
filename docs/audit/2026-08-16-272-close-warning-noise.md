# #272 — close-kill warning fired on 100% of pane closes, zero signal

## Symptom

Every `takkub done` / pane close fired `⚠️ [<role> closing] N subprocess(es)
still running under this pane are about to be killed …` to Lead, regardless
of whether the pane actually had unfinished work. On 2026-08-16 Lead received
15+ copies of this notice in one day, each ~60-80 tokens, none carrying new
information.

## Root cause

`orchestrator.py::_warn_if_live_children` (#234) only checked
`if not children: return` — but every provider's pane always has at least
one live child process at close time, because the CLI itself is launched
through a shim:

- claude / codex / opencode: npm `.cmd` shim → on Windows this spawns
  `cmd.exe`, which pywinpty gives its own `conhost.exe`, which in turn runs
  the CLI's bundled `node` runtime.
- codex additionally keeps its `code_mode`/`codex_apps` sandbox host
  (`codex-code-mode-host.exe`) alive under the pane.
- kimi-cli is `uv tool install`ed and runs on a `python` interpreter.

None of that is "unfinished work" — it is scaffolding that is present on
literally every close, so the guard's condition was true 100% of the time
and the warning carried no signal (#234's original intent — catching a
`docker compose build` still running when `done()` tears the pane down — was
correct, just written far too broadly).

## Fix

Added a 14th field group to `provider_spec.ProviderSpec`:
`scaffolding_process_names: tuple[str, ...]` — each provider's own confirmed
launcher/runtime child names (case/`.exe`-insensitive), populated from the
evidence above:

| provider | scaffolding_process_names |
|---|---|
| claude | `node`, `node.exe` |
| codex | `node`, `node.exe`, `codex-code-mode-host`, `codex-code-mode-host.exe` |
| opencode | `node`, `node.exe` |
| kimi | `python`, `python`, `python3` |
| gemini, cursor | *(none confirmed yet — left empty)* |

Plus a Windows-only cross-provider baseline
`GENERIC_SCAFFOLDING_PROCESS_NAMES_WIN32 = ("cmd.exe", "conhost.exe")`
(ConPTY's own console-host pair, not provider-specific) applied only when
`sys.platform == "win32"` — POSIX has no equivalent process and none was
observed there, so nothing is guessed for it.

Two new helpers in `provider_spec.py`:
- `normalize_process_name(name)` — lowercase, strip trailing `.exe`, so one
  table entry matches both Windows and POSIX spellings.
- `scaffolding_process_names_for(provider)` — that provider's own list plus
  the Windows baseline (gated on `sys.platform`), normalized, as a
  `frozenset`.

`orchestrator._warn_if_live_children` now resolves the pane's provider via
`provider_config.effective_provider_for(role_name, project=project_ns)` (the
same runtime-provider resolver already used elsewhere in this file),
subtracts the scaffolding set from the live children, and only notifies Lead
— with a count/name-list built from the *filtered* remainder — when
something real survives. An all-scaffolding child list now returns silently
before ever calling `_notify_lead`.

Deliberately not done (out of scope, see issue's "consider" wording): a
CPU-usage secondary signal. The provider-scaffolding filter alone already
takes the false-positive rate from 100% down to "only when unrecognized
processes remain," which covers the issue's four numbered asks (1, 2, 4) at
minimal surface area; adding a CPU-sampling heuristic would need to stay
non-blocking and had no available reproduction to calibrate against.

## Files changed

- `src/agent_takkub/provider_spec.py` — new field + per-provider values +
  `GENERIC_SCAFFOLDING_PROCESS_NAMES_WIN32` + `normalize_process_name` +
  `scaffolding_process_names_for`.
- `src/agent_takkub/orchestrator.py` — `_warn_if_live_children` filters
  children through the new helper before deciding whether to warn.
- `tests/test_progress_and_close_warning.py` — 4 new cases under
  `TestWarnIfLiveChildren`: scaffolding-only stays silent (claude), codex's
  extra `codex-code-mode-host.exe` stays silent, kimi's `python.exe` stays
  silent, and real work (`docker`) surviving the filter still warns with a
  count/detail scoped to just the real work (no #234 regression).

## Verification

Targeted tests only, run via `pytest.main(...)` in-process (the `python -m
pytest <file>` CLI form on this box goes through an unrelated caching layer
that short-circuits re-runs of unchanged files with a generic "No tests
collected" message — not a real failure, confirmed by cross-checking with
`pytest.main` directly, which shows the true dot output):

- `tests/test_progress_and_close_warning.py` — 21/21 passed (17 existing +
  4 new).
- `tests/test_auth_failure_detection.py`, `test_cursor_provider.py`,
  `test_kimi_provider.py`, `test_opencode_provider.py`,
  `test_provider_config.py`, `test_provider_spec_effort.py` — all pass
  unchanged (new dataclass field is `default_factory=tuple`, backward
  compatible).

Full suite not run per this task's targeted-tests-only scope (#project
convention) — leave to the qa batch gate before merge.

## Follow-up: cross-platform CI failure (2026-08-16, post-merge)

1.0.67's CI run (windows-latest green, macos-latest + ubuntu-latest red,
[run 31935323677](https://github.com/takkub/agent-takkub/actions/runs/31935323677))
failed 3 of the 4 new `TestWarnIfLiveChildren` cases:
`test_scaffolding_only_children_stay_silent`,
`test_codex_scaffolding_stays_silent`, `test_real_work_still_warns_past_scaffolding`.

**Root cause was in the tests, not the implementation.** All three fed
mocked children named `cmd.exe`/`conhost.exe` — the Windows-only ConPTY
console-host pair (`GENERIC_SCAFFOLDING_PROCESS_NAMES_WIN32`, correctly
gated `if sys.platform == "win32"` in `scaffolding_process_names_for`) —
without ever pinning `sys.platform`. On the CI machine's real OS
(`darwin`/`linux`), that generic set evaluates to `()`, so those two names
never landed in the scaffolding set and leaked through as "real" children:
`test_scaffolding_only_children_stay_silent`/`test_codex_scaffolding_stays_silent`
warned when they should have stayed silent, and
`test_real_work_still_warns_past_scaffolding` counted 3 subprocesses
(`node.exe`+`cmd.exe`+`conhost.exe` all miscounted alongside `docker`)
instead of the intended 1. The three hypotheses from the task were checked
in order: the win32-only baseline is deliberate and documented (not a bug —
POSIX genuinely has no ConPTY console-host analog to guess at); the per-provider
`scaffolding_process_names` entries already list both the `.exe` and
bare-name spelling (e.g. claude's `("node.exe", "node")`), so
`normalize_process_name` matches those on every OS already; the actual
defect was the third hypothesis — Windows-shaped fixture names asserted
without gating `sys.platform` to match.

**Fix** (test-only, no production code changed — the filter itself was
already correctly cross-platform):

- `monkeypatch.setattr(sys, "platform", "win32")` added to the 3 failing
  tests so they pin and prove the Windows ConPTY-baseline branch on every
  CI OS, not just whichever one happens to be running the suite.
- 3 new POSIX-side cases added (`test_scaffolding_only_children_stay_silent_posix`,
  `test_codex_scaffolding_stays_silent_posix`,
  `test_real_work_still_warns_past_scaffolding_posix`), each pinning
  `sys.platform` to `"linux"`/`"darwin"` and using bare (`.exe`-less)
  process names (`node`, `codex-code-mode-host`) with no `cmd.exe`/`conhost.exe`
  in the mix, proving the provider-owned scaffolding list alone is enough
  on POSIX and that real work (`docker`) still survives the filter and
  warns there too.
- Both branches (`win32` and POSIX) are now exercised in a single test run
  on any one OS via `sys.platform` monkeypatching, per the project's
  cross-platform convention — no longer relying on which CI runner happens
  to execute the file.

Verification: `PYTHONPATH=<repo>/src python -m pytest
tests/test_progress_and_close_warning.py -q` → 24/24 passed (17 pre-#272 +
4 from #272 + 3 new POSIX cases) on this Windows dev box. Full suite not
run per targeted-tests-only convention — leave to the qa batch gate.
