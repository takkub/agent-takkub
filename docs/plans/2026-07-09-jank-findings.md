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

## Fix applied (2026-07-09, issue #106, option (b))

`PtySession._feed_and_log` (reader thread) now classifies ready state
**inside the same `with self._screen_lock:` block** it already uses to
`stream.feed(data)` — one extra `_classify_ready(_ready_region(...))` call,
paid for by the reader thread that was already holding the lock, and stashed
in a new `self._cached_ready: bool` attribute. `PtySession.is_at_ready_prompt_cached()`
is a lock-free read of that attribute (plain bool assignment is atomic under
the GIL, same reasoning already used for `_last_output_ts`).
`agent_pane.AgentPane._sync_idle_flag` — the exact call site named in the
suspect section above, connected to `outputUpdated` and firing on every PTY
read chunk (150 ms-throttled) — now calls `is_at_ready_prompt_cached()`
instead of `is_at_ready_prompt()`, so the Qt main thread no longer takes
`_screen_lock` on this path at all. Every OTHER caller of
`is_at_ready_prompt()` (the self-healing submit verify loop, the idle
watchdog's `_maybe_submit_stuck_paste`, `_send_when_ready`'s ready poll, etc.)
is intentionally left on the locked/authoritative path — those need the
freshest possible verdict for correctness, not just a fast UI poll, and
widening the change would trade a proven-safe cached read for an unproven
one on paths where staleness could reintroduce a delivery bug. Net change in
observable behaviour: the idle-flag chip can lag the true screen state by up
to one PTY read chunk (bounded to the existing 150 ms throttle window while a
pane is actively streaming; unchanged while idle, since no new chunk means no
new classification). Tests: `tests/test_pty_session_threading.py` (cache
populated under lock, survives a `stream.feed()` exception) and
`tests/test_agent_pane_idle_flag.py` (`_sync_idle_flag` calls the cached
accessor and never the locked one — a mock that raises if the locked
`is_at_ready_prompt()` is called catches a future regression back to this
exact bug).

**Honest status — not yet re-profiled live.** This removes the specific
`_screen_lock` contention path the suspect section above named as primary
(main thread vs. reader thread racing the SAME lock on every `outputUpdated`
across every pane), but this task did not re-run the cockpit under real
multi-pane load to re-measure `main_thread_stall` counts against the 86/day
baseline — that requires a live session with panes actively streaming, which
is out of scope for this change. If stalls persist after this ships, the
remaining suspects to check next, in order: (1) `AgentPane._coalesce_bytes` /
the xterm.js render path itself (QWebEngine bridge calls stay on the main
thread and were never profiled in isolation from the lock-contention
hypothesis); (2) other `is_at_ready_prompt()` callers that DO still run on
the main thread outside `_sync_idle_flag` — e.g. anything in
`orchestrator.py`'s idle-watchdog tick that reads pane state directly rather
than through `AgentPane`; (3) `display_rich()` (used by copy/select), which
was not touched here and still takes `_screen_lock` unconditionally.
