# Cockpit freeze — root cause analysis (2026-05-29)

Diagnosed with py-spy live stack dumps + raw-socket IPC probe + A/B test.

## TL;DR

There are **two separate problems**, often confused as one "the cockpit hangs":

1. **(FIXED)** `LogsPanel` read the *entire* `events.log` every 1 s on the Qt
   main thread. Once the log reached ~10 MB this saturated the main thread →
   permanent ~100 % CPU wedge that only cleared on restart.
2. **(NOT fixed — architectural)** Everything heavy runs on the **single Qt
   main thread**, so a `takkub assign`/`spawn` (and the new pane's first burst
   of PTY output) blocks the IPC server for the spawn window → `takkub` CLI
   hits its 15 s timeout and the UI freezes *momentarily*, then **recovers on
   its own**. This is the "ค้างตอน assign" the user kept seeing.

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

## The sure fix for #2 (next round)

Goal: a `takkub assign`/`spawn` must never block the IPC reply or freeze the UI.

1. **Ack `assign`/`spawn` immediately.** Return "queued" to the CLI right away;
   perform the actual pane launch + ready-wait asynchronously (QTimer.singleShot
   / QThreadPool), not inline in the `_dispatch` handler.
2. **Offload per-pane PTY processing.** `pyte.feed` + `is_at_ready_prompt`
   full-screen render currently run on the main thread for every output burst.
   Move parsing/ready-detection to a worker, or render the ready-check from a
   cached screen snapshot instead of re-rendering live. (`_sync_idle_flag` is
   already throttled to 150 ms/pane — keep that, but the render itself is the
   cost.)
3. **Make timer file-reads cheap/off-thread.** `token_meter.read_last_usage`
   reads the session JSONL on the main thread on a timer — tail it like we did
   for `logs_panel`, or run it in a `QThreadPool` worker.
4. **`find_rtk_binary`** does `shutil.which` on every spawn — cache the resolved
   path once.

Priority: #1 (async spawn) gives the biggest UX win — the CLI/UI stop freezing
during spawns even if the pane itself takes 30 s to come up.

## How to verify a fix

- py-spy `--nonblocking` the orchestrator while firing `takkub assign`; the
  MainThread must stay parked at `app.py:117` (event loop) — never blocked in
  `spawn`/`pyte`/`read_*` for more than a frame.
- `takkub list` issued during a spawn must reply in < 1 s.
