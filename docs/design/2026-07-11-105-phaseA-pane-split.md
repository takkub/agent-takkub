# #105 Phase A — AgentPane model/view split

**Goal (issue #105, blocker 1 of 4):** the engine (orchestrator + watchdogs)
must be able to spawn and drive an agent slot without a display, so the whole
cockpit can eventually run headless in Docker with the PWA as the only UI.
Today `AgentPane` is a `QFrame` (QtWidgets) that holds `QWebEngineView`
(`TerminalWidget`) rendering AND the session/state bookkeeping the engine
reads — the two are inseparable, so the orchestrator can't construct or
drive a pane without a real Qt display stack.

Phase A is a **behavior-neutral split**: pull the session/state bookkeeping
into a new, display-free module and have the view wrap it. No behavior
change, no call-site churn for the rest of the codebase.

## What moved

New module: `src/agent_takkub/agent_pane_model.py` — `AgentPaneModel`, a
plain Python class (no `QObject`, no Qt widget/display import) importable
with nothing beyond `PyQt6.QtCore`-free stdlib + `.pty_session`/`.roles`/
`.token_meter`. Owns:

- `role`, `state`, `last_note`, `session`
- `worktree_branch`, `expected_exit`, `session_generation`
- `last_output_ts`, `tp_total_bytes` — the stuck-pane and runaway-throughput
  watchdogs' read/write surface (`orchestrator.py` `_check_stuck_panes`)
- token-meter bookkeeping: `spawn_ts`, `session_cwd`, `session_jsonl`,
  `last_usage`, `context_limit`
- `transcript_path` (set by `spawn_engine` after a pipeline hop)
- pure methods: `mark_expected_exit()`, `current_usage()`,
  `set_worktree_branch()`, `decide_exit_state(code)` (process-exit → next
  pane state, factored out of `AgentPane._on_exit`), `format_token_badge(usage)`
  (badge text/color/tooltip, factored out of `_apply_token_meter`)

Import-linter contract `agent-pane-model-headless` pins this: the module
must never import `agent_pane`, `terminal_widget`, or any UI/CLI/app entry
point (`lint-imports` — 20/20 contracts green).

## What stayed in the view

Everything that only makes sense with a display stays on `AgentPane`
(`QFrame`): the header widgets, `TerminalWidget`/xterm.js render coalescing
(`_render_buf`/`_coalesce_bytes`/`_flush_render_buf`), the spinner/elapsed
timer, auto clear-view timers, font-zoom persistence, and the token-meter's
background-thread poll + Qt signal hop back to the GUI thread
(`_tokenMeterReady` → `_apply_token_meter`) — threading/Qt-signal glue, not
headless-relevant data.

## How the split was wired with zero call-site churn

`AgentPane.__init__` now does `self.model = AgentPaneModel(role)`. Every
field that moved to the model is exposed on `AgentPane` as a `@property`
that proxies straight through (`session`, `state`, `last_note`,
`_worktree_branch`, `_expected_exit`, `_session_generation`,
`_last_output_ts`, `_tp_total_bytes`, `_spawn_ts`, `_session_cwd`,
`_session_jsonl`, `_last_usage`, `_context_limit`, `_transcript_path`).

Every other module in the codebase (`orchestrator.py`, `spawn_engine.py`,
`lead_inbox.py`, `limit_autoresume.py`, `status_header.py`, `main_window.py`,
`project_tab.py`) keeps reading/writing `pane.session`, `pane.state`,
`pane._last_output_ts`, calling `pane.attach_session(...)`,
`pane.set_state(...)`, `pane.mark_expected_exit()`, `pane.current_usage()`
exactly as before — those calls now transparently read/write through to
`pane.model` underneath. This is the "orchestrator references the model, not
the widget directly, through the existing interface" the task asked for:
the watchdog data (`_last_output_ts`/`_tp_total_bytes`) and session/state
truth now genuinely live on a display-free object; the call sites didn't
need to change because the property layer does the redirection.

Methods that mix data + view concerns (`attach_session`, `detach_session`,
`set_state`, `_on_exit`) stayed on `AgentPane` — they still do widget work
(dot color, stack index, buttons, terminal wiring) alongside the
now-model-backed data writes. `_on_exit` and `mark_expected_exit`/
`current_usage`/`set_worktree_branch` were the ones with real pure-decision
logic worth extracting; those now delegate to `model.decide_exit_state()`
etc. for testability.

## Test impact

Three white-box test helpers construct a "bare" `AgentPane` via
`AgentPane.__new__(AgentPane)` (skipping `__init__` to avoid a full Qt
widget/`TerminalWidget` build) and poke private fields directly to unit-test
one method in isolation. Since those fields are now properties requiring
`self.model`, each helper gained one line, `pane.model =
AgentPaneModel(<role>)`, before setting them — no assertion changed:

- `tests/test_pane_exit_teardown.py::_bare_pane()`
- `tests/test_render_coalesce.py::_make_pane()`
- `tests/test_fix_round2_edge_cases.py::TestAgentPaneOnExitGenerationGuard._make_pane()`

Full suite: 20/20 import-linter contracts green, ruff/ruff-format clean, full
pytest suite green (minus 7 pre-existing failures in
`test_installed_cli_bin_integration.py`/`test_installed_mode_gate.py` that
build a real venv + console-script install and fail identically on
unmodified `main` in this environment — confirmed via `git stash`/`stash
pop`, unrelated to this change).

## What's left for Phase B (per issue #105)

1. **HeadlessWindow adapter.** `MainWindow` currently owns spawn-guard/tab
   lifecycle/remote open-close flows that reach into `AgentPane` as a real
   widget (`pane.setParent(None)`, `pane.deleteLater()`,
   `pane._terminal.destroy_terminal()`, tab-widget `addTab(pane, ...)`).
   A headless engine needs a parallel adapter that holds `AgentPaneModel`
   instances directly (no `TerminalWidget`, no tab widget) and exposes the
   same lifecycle surface `Orchestrator` needs (register/close/keepalive) —
   Phase A deliberately did NOT build this; it kept `AgentPane` as the sole
   concrete pane type so nothing else had to change.
2. **Pane-registry duck-typing.** `Orchestrator._panes_by_project` is
   `dict[str, AgentPane]` throughout. Phase B would widen the accepted type
   to `AgentPane | AgentPaneModel` (or a `Protocol`) so headless mode can
   register bare models — the watchdogs already only touch the
   model-backed surface (`getattr(pane, "_last_output_ts", ...)` etc.), so
   this should be a narrow, low-risk change once HeadlessWindow exists.
3. **Ubuntu CI leg.** CI matrix is `windows-latest` + `macos-latest` only
   (`.github/workflows/ci.yml`); add `ubuntu-latest` once a headless
   entrypoint exists to exercise.
4. **Dockerfile + entrypoint.** Headless boot needs a `QCoreApplication` (or
   offscreen `QApplication`, matching what the test suite already does via
   `QT_QPA_PLATFORM=offscreen`) to keep the `QTimer` machinery
   (`AgentPane`'s timers stay view-only per this split, but the engine's own
   watchdog timers in `orchestrator.py` still need a running Qt event loop)
   alive with no window ever shown. Compose file mounts `~/.claude`/
   `~/.codex` creds + a runtime volume, exposes the remote-control port.

Sequenced after Wave 3 #6 (ProviderSpec+registry refactor) per the issue —
Phase A doesn't block or depend on that wave.
