# Running agent-takkub headless in Docker (#105 Phase B)

Headless mode boots the same orchestrator/CLI-server/remote-control engine
the desktop cockpit runs, with no window and no `AgentPane` widgets — every
pane is a `HeadlessPane` (`src/agent_takkub/headless_pane.py`) sitting inside
a `HeadlessWindow` (`src/agent_takkub/headless_window.py`) in place of
`MainWindow`. The PWA (remote-control) is the only UI surface. See
`docs/design/2026-07-11-105-phaseB-headless.md` for the architecture.

## Prerequisites

- An existing desktop cockpit login you can copy credentials from: a
  `~/.claude` directory with `.credentials.json` (from `claude login`), and
  `~/.codex` if you use the codex teammate. Headless mode has no browser to
  complete an interactive OAuth login inside the container, so it inherits
  an already-logged-in session via bind mount instead.
- Docker + Docker Compose.

## Quick start

```bash
docker compose up -d
```

This builds the image from the repo's `Dockerfile` and mounts:

- `${HOME}/.claude` → the container's default-profile
  `CLAUDE_CONFIG_DIR` (`config.default_claude_config_dir()` resolves to
  `$AGENT_TAKKUB_HOME/claude-config` once `AGENT_TAKKUB_HOME` is set, which
  the Dockerfile sets to `/data`) — so `claude` in the container is already
  authenticated.
- `${HOME}/.codex` → the container's `~/.codex` (codex reads `$HOME/.codex`
  directly; there's no `AGENT_TAKKUB_HOME`-style override for it in this
  codebase).
- a named volume at `/data` — `projects.json`, `runtime/`, custom
  `agents/`, `remote.json`, everything else `config.DATA_HOME`/
  `SETTINGS_HOME` touches, so it survives container restarts.

The container exposes port `8899` (the remote-control HTTP server the PWA
talks to — see `src/agent_takkub/remote/config.py`'s `bind_port`). `cli_server`'s own TCP
protocol (the `takkub` CLI) binds loopback-only and is only reachable from
inside the container.

## First boot: adding a project

Headless mode opens whatever projects are already in `projects.json`
(mirroring the desktop build's "reopen last session's tabs" behavior) — on
a truly first boot there's nothing there yet. Either:

- seed `/data/projects.json` before first boot (copy one from an existing
  desktop install, or write it by hand — see `config.py`'s `PROJECTS_JSON`
  shape), or
- exec into the running container and drive `takkub` directly:
  ```bash
  docker compose exec agent-takkub takkub doctor
  ```
  (the `takkub` console script is on `PATH` inside the container venv).

Once a project is registered, `HeadlessWindow.boot()` opens it and spawns
its Lead automatically on every subsequent restart.

## Enabling remote-control

Headless mode's only UI is the PWA. `RemoteControl.maybe_start()` reads
`~/.takkub/remote.json` (`remote.json` under `SETTINGS_HOME`; with
`AGENT_TAKKUB_HOME=/data`, i.e. `/data/remote.json`) same as the desktop
build — `enabled: true` has to already be set for the HTTP server to open a
socket at all. If you've never turned remote-control on from a desktop
cockpit pointed at the same `/data` volume, seed the file yourself or copy
one from a desktop install that has it configured; there is no separate
headless-only remote-control setting.

## Known limitation

`orchestrator.py` still imports `agent_pane.py` for one `isinstance()`
check (pre-Phase-B code, not something Phase B introduced), which
transitively pulls in `QtWebEngineWidgets`. The image therefore still needs
Chromium's runtime shared libraries (`libnss3`, `libgbm1`, etc. — see the
Dockerfile) even though headless mode never constructs a `QWebEngineView`
or shows a window. `QT_QPA_PLATFORM` is deliberately left unset — headless
mode uses a bare `QCoreApplication`, not the offscreen QPA plugin the test
suite uses, so no X server / `libEGL`/`xcb` is required, just the
WebEngine `.so`'s own dlopen'd dependencies.

## gemini/agy

`agy` (Google Antigravity CLI) has no scripted Linux install today, so it
isn't installed in the image. A `gemini` role assigned inside this
container degrades to a claude-substitute pane, the same
provider-unavailable path the desktop build takes when a provider is
disabled or missing (see `provider_config.effective_provider_for`).

## Stopping

```bash
docker compose down
```

`HeadlessWindow.shutdown()` (wired to `SIGTERM`/`SIGINT` in
`headless.py`) closes every open project's panes and stops
remote-control before the process exits, so `docker stop` doesn't orphan
live `claude`/`codex`/`agy` child processes.
