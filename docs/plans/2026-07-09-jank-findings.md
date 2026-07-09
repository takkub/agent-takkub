# Jank findings — main_thread_stall profiling (2026-07-09)

Scope: profile-only per task spec. No fix applied here — this is input for a
follow-up fix task.

## Data

`runtime/events.log` today: 86 `main_thread_stall` events, 765ms–6078ms
(avg ~1566ms), **100% with `spawn_in_progress: false`**. This refutes the
pane-spawn-induced-freeze hypothesis that `_StallTracker`'s comment in
`app.py` was written to test — spawn is not in flight for any of today's
stalls, so the freeze source is something that runs continuously, not only
around `takkub assign`.

`runtime/boot.log` soft-stall dumps (`faulthandler.dump_traceback(all_threads=True)`
at 1.5s+) show the recurring background-thread population per stall: N pairs
of `pty_session.py:482` (`_WriterThread` queue.get) + `_pty_backend.py:71`
(`_ReaderThread` blocking `winpty` read) per live pane, one `tunnel.py:383`
drain thread, one `limit_status.py:495` poll loop, one `socketserver.serve_forever`.
None of these dumps a busy GUI-thread frame directly — `faulthandler`'s
"Current thread" label in the dump is the watchdog daemon thread that *called*
`dump_traceback`, not the wedged Qt main thread, so the dump doesn't pinpoint
the stalled frame by itself. Treat the thread-count-scales-with-panes
correlation as circumstantial, not proof.

## Suspect: `PtySession._screen_lock` contention from `_sync_idle_flag`

`agent_pane.py:_sync_idle_flag` is connected to `PtySession.outputUpdated`,
which fires **on the Qt main thread** once per PTY read chunk
(`pty_session.py:_on_bytes`, called from the reader thread via a queued
connection). Throttled to one poll per 150ms per pane
(`_IDLE_POLL_MIN_INTERVAL`), it calls `session.is_at_ready_prompt()` →
`_ready_region(self.display_lines())` synchronously, still on the main
thread.

`display_lines()` (`pty_session.py:802`) takes `self._screen_lock` to read the
pyte screen. The SAME lock is held by the reader thread in `_feed_and_log`
(`pty_session.py:669`) while feeding potentially large PTY bursts into pyte —
this runs off-thread by design (issue #35 doc comment), but it's not lock-free
relative to the main thread's `display_lines()` reads.

With N teammate panes, each emitting `outputUpdated` up to ~6-7×/s
(150ms throttle) and each main-thread callback doing a lock-guarded
`display_lines()` + string classification, a burst of output across several
panes at once (e.g. multiple agents mid-response, or one agent dumping a long
tool result) can stack: main thread blocks briefly on `_screen_lock` per pane
while that pane's reader thread is mid-`stream.feed()`, and this repeats once
per pane per poll window. This is consistent with the observed profile —
stalls cluster in bursts (14:55:27–14:56:41 has 10 stalls in ~74s) rather than
being evenly spread, which matches "several panes talking at once" rather
than a single periodic timer.

The 5s-interval token-meter poll (`_refresh_token_meter`) is NOT a suspect —
it already moved the file glob + JSONL read to a background thread
(`_tokenMeterReady` → `_apply_token_meter`), per the comment at
`agent_pane.py:544`.

## Not yet confirmed

- No instrumentation exists to directly time `is_at_ready_prompt()` /
  `display_lines()` calls on the main thread, so the lock-contention theory
  is circumstantial (thread population + burst-clustering), not a captured
  stack frame inside the wedge itself.
- Follow-up fix work should either (a) add a cheap perf-counter wrapper
  around `_sync_idle_flag`'s call to confirm it's the wall-clock source, or
  (b) move the ready-prompt classification off the main thread the same way
  the token meter was moved (background thread + signal handback), which
  would resolve the contention regardless of whether it's the sole cause.
