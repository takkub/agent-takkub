# Cockpit freeze — root cause analysis (2026-05-29)

Diagnosed with py-spy live stack dumps + raw-socket IPC probe + A/B test.

## TL;DR

There are **three separate problems**, often confused as one "the cockpit hangs":

1. **(FIXED)** `LogsPanel` read the *entire* `events.log` every 1 s on the Qt
   main thread. Once the log reached ~10 MB this saturated the main thread →
   permanent ~100 % CPU wedge that only cleared on restart.
2. **(NOT fixed — architectural)** Everything heavy runs on the **single Qt
   main thread**, so a `takkub assign`/`spawn` (and the new pane's first burst
   of PTY output) blocks the IPC server for the spawn window → `takkub` CLI
   hits its 15 s timeout and the UI freezes *momentarily*, then **recovers on
   its own**. This is the "ค้างตอน assign" the user kept seeing.
3. **(FIXED — commit 0a84ef4)** The shared Chromium **GPU process crashes**
   when 2+ project tabs each host xterm.js WebEngine views. When it dies every
   view goes blank/white and the window stops responding — the OS shows a
   "not responding / end process" dialog. This is the **hard white-screen
   freeze** the user had to close+reopen the app to escape (distinct from #2,
   which self-recovers). Fixed by forcing software rendering
   (`--disable-gpu --disable-gpu-compositing`): a text terminal needs no GPU,
   so there is no GPU process left to take the UI down.

The orchestrator itself never crashed. During a "hang" a raw TCP request to
the IPC port replied in **0.00 s** while `takkub list` timed out — the GUI was
healthy; only the main thread was momentarily busy / the CLI gave up.

## Evidence

- **Idle baseline (py-spy):** MainThread parked at `app.py:117` (Qt event
  loop); per-pane PTY reader threads idle. Raw socket → instant reply.
- **During a spawn (py-spy samples):** MainThread seen in
  `spawn → find_rtk_binary → shutil.which` (PATH scan), `_on_bytes →
  _sync_idle_flag → is_at_ready_prompt → pyte render` (full-screen render per
  output burst), `token_meter.read_last_usage` (reads session JSONL),
  `terminal_widget._flush_writes`, `logs_panel._poll`.
- **A/B:** 10.4 MB `events.log` → assign wedged permanently; after rotating the
  log to a few KB the same assign **recovered** → log size was the trigger for
  problem #1.
- **Post-fix:** `takkub list` alone = 1 s; right after `takkub assign` it can
  time out for the spawn window, then `takkub list` works again (~5 s) and the
  new pane shows `active`.

## Architecture fact

`cli_server.py` docstring: *"Runs on the Qt main thread via QTcpServer so all
calls into Orchestrator are serialised naturally."* So the IPC server, the
whole UI, every pane's pyte parsing + ready-prompt render + transcript write,
and the file-reading timers (LogsPanel, token_meter) all compete for one
thread. Any one of them blocking starves the others.

## What was fixed (shipped, commits 430d74f / ddea1f5)

- `logs_panel.py`: read only the last 256 KiB (`_TAIL_BYTES`) via seek, never
  the whole file.
- `orchestrator._log_event`: cap `events.log` at 2 MiB, rotate to
  `events.log.old`. It can never bloat to 10 MB again.
- `prune_old_transcripts()` at startup: delete `*.transcript.log` older than
  7 days (a runaway pane had left a 203 MB transcript; `sessions/` was 547 MB).

These remove the *permanent* wedge (#1) and the disk bloat. They do **not**
fix the transient spawn freeze (#2).

## Fix for #2

Goal: a `takkub assign`/`spawn` must never block the IPC reply or freeze the UI.

1. **Ack `assign`/`spawn` immediately. — DONE (commit c0c0dc6).** The cli_server
   `_dispatch` now runs the lead-token/role gates synchronously, replies to the
   client, then schedules the actual spawn via `QTimer.singleShot(0)`. Verified
   live: `takkub assign` returns in **221 ms** (was ~15 s timeout) and `takkub
   list` right after stays responsive at **257 ms** with the pane spawning async.
   This removes the CLI-timeout / retry pile-up that was the visible "freeze".
2. **Offload per-pane PTY processing.** `pyte.feed` + `is_at_ready_prompt`
   full-screen render currently run on the main thread for every output burst.
   Move parsing/ready-detection to a worker, or render the ready-check from a
   cached screen snapshot instead of re-rendering live. (`_sync_idle_flag` is
   already throttled to 150 ms/pane — keep that, but the render itself is the
   cost.)
3. **Make timer file-reads cheap. — DONE (commit dc6fa14).**
   `token_meter.read_last_usage` now tail-reads the last 512 KiB of the session
   JSONL (full-scan fallback) instead of streaming the whole file every 5 s.
4. **`find_rtk_binary` — DONE (commit dc6fa14).** Caches the resolved path
   (re-validated; negatives not cached) so spawns don't re-scan PATH.

## Fix for #3 — DONE (commit 0a84ef4)

Add `--disable-gpu --disable-gpu-compositing` to `QTWEBENGINE_CHROMIUM_FLAGS`
in `app.py` (before QtWebEngine boots). Forces software compositing so no GPU
process exists to crash.

Verified live: `gpu-process` count = **0**, 8 panes + multiple project tabs
open, `takkub list` responsive at **128 ms**, and the user confirmed the
white-screen freeze no longer reproduces when opening 2+ projects. Memory cost
is acceptable (terminal text rendering is cheap on CPU); no perceptible latency.

Remaining: only #2 (offload pyte parsing/render off the main thread). With #1
shipped the CLI/UI no longer freeze *waiting* on a spawn; the pyte parse +
transcript write are already moved to the reader thread (commit 01633c7), so
the main thread now only forwards bytes to xterm.js + emits the
state-change notify. The per-spawn QWebEngine init is inherent to using
WebEngine and is not addressed by any of the above.

## How to verify a fix

- py-spy `--nonblocking` the orchestrator while firing `takkub assign`; the
  MainThread must stay parked at `app.py:117` (event loop) — never blocked in
  `spawn`/`pyte`/`read_*` for more than a frame.
- `takkub list` issued during a spawn must reply in < 1 s.
- **White-screen (#3):** with the running app, no `QtWebEngineProcess.exe`
  command line should contain `type=gpu-process` (count must be 0). Open 2+
  project tabs, spawn several panes each — views must keep rendering (never go
  blank/white) and the window must stay responsive.
