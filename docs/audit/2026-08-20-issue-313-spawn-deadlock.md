# Issue #313 — main-thread deadlock when a native pty spawn races an npm self-update

**Status:** root cause proven by direct reproduction against this repo's own dependency. Fix implemented in the same PR — see "Fix" section.

## Symptom (from the issue)

`takkub status`/`takkub list` timed out continuously for ~3 hours after an npm auto-update
of `@anthropic-ai/claude-code` landed mid-session. A pane's native pty spawn of `claude.exe`
raced the update's file write; Windows popped a modal "Unsupported 16-Bit Application" hard-error
dialog for the corrupted binary, and the whole cockpit (every pane, not just the one that issued
the spawn) went unresponsive until the incident resolved itself hours later. `boot.log` — which
already has a watchdog daemon whose whole job is to dump a wedged main-thread stack — stayed
completely silent for the entire incident.

## Call path (proof, part 1: static trace)

```
spawn_engine.py  Orchestrator.spawn() / _launch_session()  [Qt main thread]
  └─ pty_session.py  PtySession.spawn()                    [Qt main thread, synchronous]
       └─ _pty_backend.py  spawn_pty_bounded(..., timeout_sec=30)
            └─ worker = threading.Thread(target=_run); worker.start(); worker.join(timeout_sec)
                 └─ [worker thread] _pty_backend.py  spawn_pty()
                      └─ _WinptyBackend.spawn()  (Windows)
                           └─ winpty.PtyProcess.spawn()          [site-packages/winpty/ptyprocess.py:111]
                                └─ PTY.spawn(...)                 ← compiled C extension (winpty.cp311-win_amd64.pyd)
                                     └─ Windows CreateProcess(claude.exe, ...)
```

`spawn_pty_bounded` already exists from issue #139 specifically to bound this exact native
constructor call: it runs `spawn_pty()` on a worker thread and the *calling* thread only waits
up to `PTY_SPAWN_TIMEOUT_SEC` (default 30 s) via `worker.join(timeout_sec)`, on the theory that a
wedged native call can then be abandoned instead of hanging the caller forever.

**That bound did not save the incident.** The reason is the GIL, not a bug in `spawn_pty_bounded`'s
own logic.

## Root cause (proof, part 2: direct reproduction)

`winpty.cp311-win_amd64.pyd` is a compiled C extension. Whether `PTY.spawn()`'s blocking
`CreateProcess` call releases the GIL while it blocks is not documented and can't be determined
from the outside without either its source or an empirical test — so it was tested directly,
against the exact dependency version installed in this repo's environment (`pywinpty` 3.0.3).

**Test 1 — does the native call hold the GIL hostage?**

A corrupt "executable" (`MZ` signature + zero-filled body, no valid PE header — the same shape an
npm write caught mid-flight would leave) was spawned via `winpty.PtyProcess.spawn()`, with a
`threading.Thread` failsafe armed to call `os._exit(2)` after 12 s if the process was still alive
(i.e. exactly the kind of independent "watchdog thread" this codebase's own `_start_deadman_watchdog`
already relies on). The process did **not** exit — it was still alive well past the 12 s failsafe's
deadline (verified 120 s+), meaning even that failsafe thread could not run to completion. A plain
`threading.Thread` doing `time.sleep()` + `os._exit()` needs to reacquire the GIL to execute its own
Python bytecode after waking up; if it never got the CPU to do so, the GIL was not released by
whatever was blocking in the other thread. This directly confirms: **the native spawn call does not
release the GIL while blocked**, so a wedge inside it freezes every Python thread in the process —
not just the one that issued the call.

(A follow-up `SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX)`
was also tried before the same spawn, on the theory it might make `CreateProcess` return
`ERROR_BAD_EXE_FORMAT` immediately instead of showing the dialog. It did **not** help — the process
still hung. Windows' "Unsupported 16-Bit Application" dialog for this specific failure mode is not
governed by `SetErrorMode`, or pywinpty's own spawn path doesn't reach that far before wedging. This
was confirmed live — the dialog actually appeared on screen during the *first* test, before
`SetErrorMode` was tried — so **suppressing the dialog is not a viable primary fix**, and the
codebase should not rely on it.)

**Test 2 — does `faulthandler.dump_traceback_later` survive the same hang?**

The same corrupt-exe spawn was repeated, but this time with
`faulthandler.dump_traceback_later(4, repeat=False, file=<f>, exit=False)` armed immediately
beforehand. The process was still wedged (force-killed by an external, self-contained
`Start-Process` + `WaitForExit(timeout)` harness after 15 s, confirming the hang independently of
Test 1), but the dump file **did** get written, with a real stack:

```
Timeout (0:00:04)!
Thread 0x0000be8c (most recent call first):
  File "...\site-packages\winpty\ptyprocess.py", line 111 in spawn
  File "...\gil_test.py", line 12 in <module>
```

This is the direct, empirical confirmation of both the exact call-site (`ptyprocess.py:111`,
matching the static trace above) and that `faulthandler.dump_traceback_later()`'s C-level watcher
thread fires and produces a usable dump **even while every other Python thread in the process is
frozen** — because unlike `_start_deadman_watchdog`'s existing `threading.Thread`-based dumper
(which calls `faulthandler.dump_traceback()` as a normal, GIL-requiring Python call), the "later"
variant is armed via `PyThread_start_new_thread` at the C level and does not need to reacquire the
GIL to fire.

## Why the existing watchdog went silent (proof, part 3: same root cause)

`app.py`'s `_start_deadman_watchdog` daemon thread is the mechanism that was supposed to leave a
"main thread wedged" trail in `boot.log`. Its dumps go through `_dump_main_stack()`, which calls
`faulthandler.dump_traceback(file=_BOOT_LOG_FH, all_threads=True)` **directly as a synchronous
Python call** — this needs the GIL to even be entered. Test 1 above proves that once the native
spawn call holds the GIL hostage, no other Python thread — this watchdog daemon included — can run
at all. That is a complete, direct explanation for the issue's own observation: *"boot.log stopped
being written to well before this incident started (its watchdog-dump thread presumably also
blocked)"*. It wasn't a coincidence or a separate bug; it's the same GIL-hostage condition disabling
every piece of the app's own instrumentation simultaneously.

## What was ruled out

- **`SetErrorMode` alone as the fix** — tested directly, did not prevent the hang or the dialog (see
  Test 1). Not used as the primary mitigation.
- **A "real" main-thread timeout via `threading.Thread` + `join(timeout)`** (the existing #139
  design) — sound in theory, but empirically defeated by the GIL: the join's own OS-level wait
  completes on schedule, but the calling thread cannot resume executing Python bytecode afterward
  because it must first reacquire a GIL that the wedged worker thread is still holding.
- **Moving the actual native spawn call to a fully separate OS process** (so a hang there can never
  touch the main process's GIL at all) — this is the only *airtight* structural fix, but pywinpty's
  `PtyProcess` owns the live ConPTY read/write handles directly in the process that created them
  (`self.pty`, a native C++ object); marshaling that ownership across a process boundary would
  require changes pywinpty itself doesn't expose, and is out of proportion to a targeted fix. Not
  attempted here — see "Residual risk" below.

## Fix

Three changes, matching the issue's own three suggested directions:

**1. Prevent the trigger (primary fix).** `_pty_backend.spawn_pty()` now validates `argv[0]`
*before* ever calling into the native constructor: it resolves the target via the same PATH lookup
pywinpty/ptyprocess use internally, then reads its header — a Windows PE (`MZ` + a valid `PE\0\0`
signature at `e_lfanew`) or a POSIX ELF/Mach-O/shebang — and raises `SpawnTargetCorrupt` (pure
Python, in-process, no OS call involved) instead of calling the native constructor when a file that
should be a native binary has a missing/truncated header. This is pure file I/O — sub-millisecond,
can never hang, and applies to every provider (claude/codex/gemini/opencode/kimi/cursor all funnel
through the same `spawn_pty()`) and both platforms (Windows PE check / POSIX ELF+Mach-O+shebang
check, each gated on `sys.platform`). It does not close the race window entirely (the file can still
be rewritten in the gap between this check and the real constructor call a few lines later), but it
shrinks that window from "however many seconds npm takes to rewrite the whole file" down to a few
milliseconds — the same TOCTOU-narrowing pattern this codebase already uses for the unrelated
`InSendMessageEx` gate (`spawn_gate.is_in_send_stable`, Tier 1/Tier 2).

**2. Retry/backoff.** `spawn_engine.py`'s two spawn call sites (`_launch_session` for
shell/gemini/codex, and the inline claude branch) now catch `SpawnTargetCorrupt` ahead of the
generic exception handler and re-queue the spawn through the existing deferred-spawn machinery
(`_spawn_deferred` + `_retry_deferred_spawn`) with a linear backoff (300 ms × attempt number, capped
at 5 attempts / ~4.5 s total) instead of failing the pane outright — an npm rewrite finishes in low
single-digit seconds, well inside that budget. A per-pane `PaneState.corrupt_spawn_retries` counter
resets to 0 on the next successful spawn so a rare, unrelated later corruption isn't penalized by a
stale count.

**3. HARD-stall watchdog, independent of the SOFT-stall class.** `app.py` now arms
`faulthandler.dump_traceback_later(_HARD_STALL_TIMEOUT_S, repeat=False, file=boot.log, exit=False)`
at startup and re-arms it every `_HARD_STALL_REARM_INTERVAL_S` (60 s) from inside the existing
deadman-watchdog daemon loop, as long as the main-thread heartbeat is healthy. This is a genuinely
different mechanism from the existing SOFT-stall dump (which, per Test 1 + the "why the existing
watchdog went silent" section above, is a plain Python call that can itself be starved of the GIL):
`dump_traceback_later`'s C-level watcher thread doesn't need the GIL to fire, so if the main thread
ever goes silent for `_HARD_STALL_TIMEOUT_S` straight — this incident's class of hang, or any other
cause — the last-armed deadline fires on its own and boot.log gets a real all-threads dump instead
of nothing. Consistent with this codebase's existing "never auto-kill a wedge, only make it
diagnosable" policy (see `_start_deadman_watchdog`'s own docstring) — this doesn't try to recover
the process, only to stop the log from going silent exactly when it matters most.

## Residual risk (explicitly not fixed)

Fix #1 closes the actual reproduced trigger (a corrupted/mid-write executable) to a few-millisecond
race window, but does not make the GIL-hostage *mechanism* itself impossible — a native call could
still hang for some other reason we haven't seen and hold the process hostage the same way. Given
the analysis above, only moving the spawn off-process entirely would close that mechanism for good,
and that's judged out of proportion for this fix (see "What was ruled out"). Fix #3 is the deliberate
mitigation for this residual risk: it can't prevent a future hang of this class, but it guarantees one
gets a stack dump in `boot.log` within `_HARD_STALL_TIMEOUT_S`, instead of the ~3 hours of total
silence seen in the actual incident.

## Test artifacts

The two live reproductions above (corrupt-exe spawn via `winpty.PtyProcess.spawn`) were run against
a throwaway file in the session scratchpad, each wrapped in a self-contained, PID-scoped kill switch
(never a name-based kill) so nothing was left hanging afterward; both were confirmed cleaned up (no
leftover process, no leftover dialog window) before continuing. They are **not** committed as
automated regression tests — deliberately: a regression test that actually spawns a corrupt exe risks
popping the same real OS modal dialog in CI (which would hang a `windows-latest` runner solid), so
automated coverage instead targets the pieces that don't require reproducing the OS-level hang itself:
the header-validation logic (`_looks_like_valid_executable`) against synthetic good/bad files, and the
retry/backoff path via a monkeypatched `SpawnTargetCorrupt`.
