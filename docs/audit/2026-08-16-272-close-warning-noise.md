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
