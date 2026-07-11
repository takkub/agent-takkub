# #105 Phase B — headless mode

**Goal (issue #105, blocker 1 of 4, continuing Phase A):** the engine must
actually boot and run with zero display — no `MainWindow`, no `AgentPane`
widget, no `QWebEngineView` — so the whole cockpit can run in Docker with
the PWA (remote-control) as the only UI. Phase A (merged) split
session/state bookkeeping into a display-free `AgentPaneModel`; Phase B
builds the parallel adapter stack that actually drives one and wires it
into a real boot path.

## What shipped

### 1. `HeadlessPane` (`src/agent_takkub/headless_pane.py`)

A `QObject` wrapping `AgentPaneModel` (Phase A) that exposes the exact
attribute/method/signal surface the engine calls on a real `AgentPane`:
`role`/`session`/`state`/`last_note`, `set_state()`, `attach_session()`,
`detach_session()`, `mark_expected_exit()`, `current_usage()`,
`set_worktree_branch()`, and the `spawnRequested`/`closeRequested`/
`inputBytes` signals `spawn_engine.register_pane()` connects to.
`attach_session`/`detach_session`/`_on_exit` are the data-only halves of
`AgentPane`'s same-named methods (no terminal resize/focus/token-label
widget work) — same session-generation guard against stale exit callbacks,
same `bytesIn`/`processExited` wiring.

The three signals exist purely so `register_pane()`'s `.connect()` calls
succeed; headless mode never emits them (no pane header buttons, no local
keyboard capture) — all headless input (`takkub` CLI, PWA remote-control)
already writes straight to `pane.session.write(...)`.

### 2. `HeadlessWindow` (`src/agent_takkub/headless_window.py`)

`MainWindow`'s display-free counterpart: owns the `Orchestrator` + every
open project's panes with no widget anywhere.

- `_ensure_teammate_pane`/`_remove_teammate_pane` mirror `main_window.py`'s
  same-named methods, wired to `orch.paneRequested`/`paneClosed` — every
  teammate becomes a `HeadlessPane`, custom/shard roles get the same
  fallback-`Role`/label-suffix handling the desktop build does.
- `_open_project_tab`/`_close_project_tab` mirror the desktop build's tab
  lifecycle (register Lead pane, spawn it, persist open tabs) — these are
  the methods `remote/api.py` reaches dynamically via `orch.parent()` for
  the PWA's project open/close control-mode actions, so no `remote/`
  changes were needed.
- `boot()` binds the CLI server, starts remote-control if
  `~/.takkub/remote.json` has `enabled: true`, and reopens every
  previously-open project tab (same "reopen last session" behavior as the
  desktop build).
- `shutdown()` (wired to SIGTERM/SIGINT in `headless.py`) closes every open
  project so a `docker stop` doesn't orphan live claude/codex/agy children.
- Deliberately no spawn-gate predicate (`_spawn_gate_pred=None`, the
  documented no-guard path) — headless mode has no Qt modal to guard ConPTY
  spawn against.

### 3. Pane-registry duck-typing (`orchestrator.py`, `spawn_engine.py`)

Widened `dict[str, AgentPane]` to `dict[str, AgentPaneLike]`
(`AgentPaneLike = AgentPane | HeadlessPane`) at `_project_panes`,
`_project_ns_for_pane`, `panes`, and `register_pane`'s parameter type;
widened the one `isinstance(pane, AgentPane)` runtime check to
`isinstance(pane, AgentPane | HeadlessPane)`. No other call site needed to
change — every watchdog/lookup already only touches the model-backed
surface both pane types share.

### 4. Headless entrypoint (`src/agent_takkub/headless.py`)

`python -m agent_takkub.headless` / the `agent-takkub-headless` console
script (`pyproject.toml`). Boots a bare `QCoreApplication` (not
`QApplication` — no `QWidget` is ever constructed, so no GUI platform
plugin is needed), loads custom roles (same boot-order requirement
`app.py` enforces), constructs `HeadlessWindow`, installs SIGTERM/SIGINT
handlers that call `shutdown()`, and calls `boot()`. A boot failure logs to
`events.log` (the headless equivalent of `main_window`'s
`_handle_cli_bind_error` — no `QMessageBox`, there's no display) and
returns exit code 1 instead of showing a dialog.

### 5. Dockerfile + docker-compose.yml

`Dockerfile`: `node:20-bookworm-slim` base (npm-installs
`@anthropic-ai/claude-code` + `@openai/codex`), python3.11 venv, the
Chromium runtime shared libs `QtWebEngineWidgets` still dlopens even though
no window is ever shown (see "known limitation" below), `ENTRYPOINT
["agent-takkub-headless"]`.

`docker-compose.yml`: named volume at `/data`
(`AGENT_TAKKUB_HOME=/data` — covers `projects.json`, `runtime/`, custom
`agents/`, `remote.json`), bind-mounts `${HOME}/.claude` onto the
container's default-profile `CLAUDE_CONFIG_DIR`
(`config.default_claude_config_dir()` resolves to
`$AGENT_TAKKUB_HOME/claude-config` once `AGENT_TAKKUB_HOME` is set) and
`${HOME}/.codex` onto the container's own `~/.codex`, so the container
inherits an already-logged-in session instead of needing an interactive
OAuth login with no browser available. Full walkthrough:
`docs/guides/2026-07-11-headless-docker.md`.

### 6. Ubuntu CI leg (`.github/workflows/ci.yml`)

Added `ubuntu-latest` to the `lint-and-test` matrix (now Windows + macOS +
Linux). A Linux-only step installs the same Chromium runtime libs the
Dockerfile needs (no display server on the runner — `conftest.py` already
sets `QT_QPA_PLATFORM=offscreen`, but the offscreen QPA plugin still
dlopens WebEngine's Chromium dependencies to import successfully) before
the shared `pip install -e .[dev]` / lint / test steps run unchanged.
`installed-gate` stays Windows + macOS only — unaffected by Phase B, no
Linux-specific packaging behavior to verify.

## Known limitation (carried over, not introduced by Phase B)

`orchestrator.py` still imports `agent_pane.py` for one `isinstance()`
check, which transitively pulls in `QtWebEngineWidgets` — so both the
Docker image and the new ubuntu CI leg need Chromium's runtime shared libs
even though headless mode never constructs a `QWebEngineView`. Fully
removing this would mean orchestrator no longer importing `agent_pane` at
all, which is a larger refactor than Phase B's scope (see Phase A's design
doc — Phase A deliberately kept `AgentPane` as the sole concrete pane type
so nothing else had to change; Phase B only widens the accepted type, it
doesn't remove the old one).

## Test impact

New: `tests/test_headless_pane.py` (14 cases — role/state, `attach_session`
binding, `detach_session` disconnects, `_on_exit` expected/unexpected/
stale-generation, `current_usage`, `set_worktree_branch`, signal wiring),
`tests/test_headless_window.py` (12 cases — open/close project tab
idempotency and teardown, teammate pane create/ignore-lead/unknown-project/
shard/custom-role-fallback, deferred-unregister-on-next-tick,
`paneRequested` signal wiring), `tests/test_headless_entrypoint.py` (2 cases
— boot failure logs to events.log + returns 1, boot success runs the real
Qt event loop and returns 0).

Import-linter: two new contracts, `headless-pane-no-display` and
`headless-window-no-display` (22/22 contracts green, up from Phase A's
20/20).

## Full suite status

Ruff lint + format clean. 22/22 import-linter contracts green. Full pytest
suite: same 7 pre-existing failures as Phase A's documented baseline
(`test_installed_cli_bin_integration.py` / `test_installed_mode_gate.py` —
build a real venv + console-script install, fail identically on unmodified
`main`), plus two failures confirmed unrelated to this change:

- `tests/test_settings_management_roles.py::TestCharacterizationForcedProviders::test_lead_forced_provider_survives_repository_get`
  — fails identically with this branch's tracked changes stashed (i.e. on
  the unmodified base commit); lives in `settings_management/`, which this
  task was explicitly told not to touch (in-flight redesign, under
  critic review).
- `tests/test_pane_tools_dialog.py::test_matrix_roles_covers_expected_builtin_roles`
  — passes in isolation on the base commit; only fails as part of a full
  suite run because `tests/test_teammate_tier.py` registers custom roles
  ("maintainer", "test-critic-smoke") without tearing them down, leaking
  into the process-wide role registry that `matrix_roles()` reads later in
  the same session. Pre-existing test-isolation gap, not touched by
  anything in Phase B (`headless_pane`/`headless_window`/`headless.py`
  never call `custom_roles.register`).

Neither failure is caused by or fixed by this change; both are flagged here
rather than silently left out of the failure count.

## What's left (per issue #105, beyond Phase B's scope)

- Removing `orchestrator.py`'s `agent_pane` import entirely so the Docker
  image and CI's Linux leg no longer need Chromium runtime libs at all —
  would need the one remaining `isinstance()` check reworked, out of scope
  for "make headless mode boot," in scope for a future cleanup pass.
- `agy` (Google Antigravity CLI) has no scripted Linux install — a
  `gemini` role in the container degrades to a claude-substitute pane
  (same provider-unavailable path the desktop build already has).
