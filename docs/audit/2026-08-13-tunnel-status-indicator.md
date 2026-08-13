# Tunnel status indicator (top-left of the main window)

**Date:** 2026-08-13 · **Branch:** `wt/backend-1786595973`

## What

Added a small status dot at the sidebar's top-left header row (next to the
"PROJECTS" label — the true top-left corner of the main window, since the
sidebar sits flush against the window frame) that shows whether the
cloudflared/ngrok tunnel (`remote/tunnel.py`) is actually up right now.

Deliberately **separate** from the existing 🌐 Remote status-bar chip (bottom
of the window): that chip answers "is remote control (the HTTP server) on",
this dot answers "is the tunnel process itself alive right now" — a narrower,
higher-signal question, since the tunnel is the piece that was recently found
to die silently (fixed via `_verify_named_started` + `RemoteControl.tunnel_error`
in an earlier session on this branch).

## States

- **Neutral (gray)** — remote control off, or on with no tunnel
  configured/started.
- **Green** — tunnel process alive (`Tunnel.is_alive`, a fresh `poll()`, not a
  cached flag).
- **Red** — tunnel failed to start (`RemoteControl.tunnel_error`) or died
  after a clean start (`Tunnel.is_alive is False` with no `tunnel_error` set —
  falls back to `Tunnel.last_output`, the tail of the child's drained
  stdout/stderr, for a "stopped unexpectedly: <reason>" tooltip).

Hover shows the reason; no click action (pure passive readout, matching the
existing graft/overage chips' pattern).

## Changed files

- `src/agent_takkub/remote/tunnel.py` — added `Tunnel.is_alive` (property,
  fresh `poll()`) and `Tunnel.last_output` (joined drained-stdout tail,
  already collected by `_drain_output` for named-tunnel mode).
- `src/agent_takkub/project_nav.py` — sidebar header row gained a small
  `QLabel` dot (`#tunnelIndicator`) + `ProjectNav.set_tunnel_status(state,
  tooltip)` to repaint it. Pure presentation; no knowledge of `RemoteControl`.
- `src/agent_takkub/status_header.py` — `_refresh_tunnel_indicator()` (called
  from the existing `_refresh_remote_chip`, which already runs on the 2s
  status timer + every `statusChanged`) reads `self._remote` /
  `self._remote._tunnel` and decides the state/tooltip.

No new state was introduced — this reuses `RemoteControl._tunnel`,
`RemoteControl.tunnel_error`, and the newly-added `Tunnel.is_alive`/
`last_output`, per the task's "don't duplicate state" instruction.

## Tests

- `tests/test_remote_tunnel.py::TestIsAliveAndLastOutput` (5 cases)
- `tests/test_remote_chip.py::TestRefreshTunnelIndicator` (6 cases)
- `tests/test_project_nav.py::TestTunnelIndicator` (5 cases)

All targeted tests pass (68 total across the 3 files). `ruff check` clean.
`lint-imports`: 23/23 contracts kept (no new cross-module edges — this stays
inside the existing `status-header-layer`/`remote-bolt-on-isolation`
boundaries; `project_nav.py` never imports `remote`, only exposes a generic
`set_tunnel_status(state, tooltip)` setter that `status_header.py` drives).

## Not done / follow-ups

- No browser/visual verification — that's QA's job per the pane's operating
  rules (browser tools are QA-only in this cockpit). Flagging so Lead can
  route a visual check if desired.
