# Cockpit Freeze RCA — 2026-06-04

> **⚠️ STATUS (2026-06-04, latest): TWO DISTINCT "freeze" issues — don't conflate.**
>
> | | Issue A — CLI pile-up | Issue B — GUI main-thread freeze |
> |---|---|---|
> | Symptom | `takkub assign/spawn` blew the 15 s client timeout → Lead retried → pile-up felt like "ค้างตลอด" | whole window busy/Not-Responding cursor ~12 s during a pane spawn |
> | Root cause | spawn reply waited for the pane to come up, **on the CLI reply path** | native winpty **ConPTY** `CreatePseudoConsole` COM call blocks the Qt main thread ~12 s **holding the GIL** (py-spy proof; `RPC_E_CANTCALLOUT` 0x8001010d) |
> | Status | **✅ FIXED — `c0c0dc6` (2026-05-29)**: ack immediately + defer spawn via `QTimer.singleShot(0)` off the reply path. Verified 2026-06-04: `takkub assign` returns ~120 ms async. | **❌ STILL OPEN.** Both remedies reverted (off-thread = GIL-starves anyway; WinPTY backend = scrapes screen, live-typing lag). `pty_session.py:226` is still `Backend.ConPTY` synchronous on the main thread. |
>
> **2026-06-04 stress test (this session):** spawned 1 pane (frontend) then 4
> concurrent panes (backend/qa/reviewer/devops). All acked async (~120 ms CLI),
> all reached `working` together, all ran `node --version` and reported `done`
> cleanly, no zombies. This **confirms Issue A is fixed** and the orchestrator
> survives concurrent spawns. It does **NOT** prove Issue B fixed: Issue B is the
> *GUI busy-cursor* symptom (only observable at the window, not via CLI) and
> is **intermittent** (fires only when the ConPTY COM call collides with an
> input-synchronous message). B simply **did not reproduce today** — encouraging,
> not proof. A ConPTY-preserving fix for B is still needed.
>
> The WinPTY-backend fix (#2) below was tried and **reverted**: WinPTY
> screen-scrapes the buffer instead of streaming ANSI, so live typing /
> interaction felt laggy and produced odd replace-char artifacts ("ไม่ลื่น,
> แปลกๆ") — unacceptable UX. **ConPTY is retained** (the original committed
> behaviour). The root-cause analysis below is still valid; only the chosen
> *remedy* changed. A freeze fix that keeps ConPTY's live-typing feel is still
> needed — see "Open: next directions" at the bottom.

**Symptom:** Whole cockpit window freezes (Windows busy/loading cursor over the
entire app), unrecoverable → user force-restarts. Intermittent. Reported again
2026-06-04 ("ปัญหาค้างกลับมาอีกแล้ว") while two project tabs were open
(agent-takkub + unirecon) right after a new pane was spawned.

## Root cause (confirmed)

`winpty.PtyProcess.spawn()` (ConPTY backend) runs **synchronously on the Qt main
thread** during pane spawn. The ConPTY startup makes a COM/RPC call. When that
call lands while the main thread is dispatching a Windows **input-synchronous**
message (a `SendMessage`-style pump, common with QtWebEngine/xterm.js panes),
Windows rejects it with:

```
Windows fatal exception: code 0x8001010d   (RPC_E_CANTCALLOUT_ININPUTSYNCCALL)
```

> "An outgoing call cannot be made since the application is dispatching an
> input-synchronous call."

The rejected COM call leaves the ConPTY handshake half-initialized; the spawn
stalls on the main thread → the entire UI wedges → busy cursor → restart.

### Exact fail path (captured stack, current process PID 3856)

```
Current thread (MAIN):
  winpty/ptyprocess.py:96   in spawn          ← COM/RPC call rejected here
  pty_session.py:226        in spawn          ← winpty.PtyProcess.spawn(..., ConPTY)
  orchestrator.py:1650      in spawn          ← session.spawn(argv=..., cwd=..., env=...)
  main_window.py:1025       in _boot          ← startup pane restore
  app.py:229                in main
```

Same signature appears on essentially every boot in `runtime/boot.log` (159×
`0x8001010d`, the large majority with the main thread inside `winpty … spawn`).

## Evidence ledger

| # | Observation | Reading |
|---|---|---|
| B1 | `takkub list` after spawning a pane → pane never appeared | spawn never completed / instance restarted |
| B2 | `events.log` stops logging at the freeze, process still alive | main-thread event loop wedged (daemon threads/log still run) |
| B3 | Two pythonw started together 08:53:35 (restart) | prior instance died/was-killed → relaunch |
| B4 | Two project tabs (agent-takkub + unirecon) open, heavy pane activity | more spawns + more input dispatch = higher COM-collision odds |
| B5 | "busy cursor over whole window" | classic Qt main-thread block |
| B6 | `boot.log`: 159× `0x8001010d`, main thread in `winpty spawn` | COM rejection during spawn on the main thread |
| B7 | Isolated `winpty ConPTY spawn` = 5–28 ms (avg 12) | spawn is **not** slow per se — the freeze is the COM-during-input-dispatch collision, not raw spawn latency |

### False breadcrumbs ruled out (mantra step 3/4)

- **"main thread wedged for 1s" (122×) in boot.log** — emitted by an *older*
  watchdog with a 1 s threshold. Current code is `_WATCHDOG_TIMEOUT_S = 30.0`,
  so these cannot be from the running build. Stale append-log noise; **ignored**.
- **"spawn blocks the main thread for 30 s"** — disproved by B7 (12 ms baseline).
  The wedge is the COM rejection/half-init, not spawn duration.
- **0x8001010d is fatal** — disproved: PID 3856 hit it during `_boot` and is
  still alive. It is a first-chance exception faulthandler loudly dumps; the
  damage is the stalled spawn it interrupts, not process death.

## ✅ CONFIRMED root cause + fix (2026-06-04, py-spy proof)

**Definitive evidence (py-spy, external sampler — works on a GIL-frozen process):**
While a teammate-pane spawn froze the cockpit, `py-spy dump --pid <cockpit> --native`
sampled it 40× over ~16 s. **31/40 samples** showed:

```
Thread MainThread (active+gil):
    ... winpty.cp311-win_amd64.pyd  (native winpty frames)
Thread "cockpit-deadman"        (idle)   ← watchdog GIL-starved, can't fire
Thread "Thread-N (_read_in_thread)" (idle)
```

So the main thread sits in the **native winpty (ConPTY) call for ~12 s while
holding the GIL**. That single fact explains everything:
- whole window freezes (Qt loop dead, main thread in a native call);
- the dead-man watchdog never fires — it is a *Python* thread, GIL-starved by
  the held GIL, so a Python watchdog **fundamentally cannot catch a GIL-holding
  native freeze** (this is why the 30 s kill and the soft-stall both stayed silent);
- the off-thread fix (#1) failed — the worker holds the GIL instead, main starves;
- the `0x8001010d` (RPC_E_CANTCALLOUT) is the proximate trigger: ConPTY's
  `CreatePseudoConsole` makes a COM call that Windows rejects during
  input-synchronous message dispatch, so it retries/blocks for seconds.

**FIX #2 (TRIED → REVERTED 2026-06-04):** Switched `PtySession.spawn()` to
**`winpty.Backend.WinPTY`** (CreateProcess, no `CreatePseudoConsole` COM, ~50 ms,
no freeze). **Rejected in practice:** WinPTY screen-scrapes the buffer instead of
streaming ANSI, so live typing / interaction lagged and showed replace-char
artifacts ("ไม่ลื่น, แปลกๆ"). The user judged the degraded live-typing UX worse
than the (intermittent) freeze, so the change was reverted and **ConPTY is kept**.
Tests were green and the isolated spawn measured 49 ms — the blocker was purely
the interactive feel, not correctness. **The freeze therefore remains unfixed.**

### Open: next directions (keep ConPTY's ANSI streaming, kill the freeze)

The constraint is now explicit: any fix must preserve ConPTY (live ANSI stream),
not WinPTY. Candidate angles, none yet validated:
1. **Spawn ConPTY in a separate helper *process*** (not thread — GIL-bound native
   call defeated the QThread attempt #1) and hand the PTY handles back over IPC.
   Heaviest, but fully removes the COM-on-main-thread collision.
2. **Defer/serialize the spawn out of input-synchronous dispatch** — e.g. queue
   spawns to fire from a `QTimer.singleShot(0)` only when no input-sync message
   is being pumped, so `CreatePseudoConsole`'s COM call isn't rejected.
3. **Pre-warm a pool of ConPTY pseudoconsoles at idle** (before any xterm.js pane
   is dispatching input) and attach `claude.exe` to a pre-created one on demand.
4. **Confirm whether the native winpty ConPTY call releases the GIL** — if a build
   /flag exists that does, the off-thread approach (#1) could be revisited.

The instrumentation from fix #1 is kept (watchdog 30 s stack dump + boot.log
rotation + a soft-stall dump) — though note the soft-stall, being Python-thread
based, cannot catch GIL-holding freezes; py-spy `--native` is the tool for those.

---

## ⚠️ Correction (2026-06-04): off-thread spawn (fix #1) REVERTED — did not fix it

The off-thread-spawn fix below was shipped, then **reverted**: spawning a real
claude teammate pane still froze the UI (transient, recovered before the 30 s
kill, so the watchdog never dumped). The live boot dump confirmed the winpty
call had moved to the worker thread (`_SpawnWorker`) with the main thread parked
in `loop.exec()` — yet the freeze persisted. Leading hypothesis now: the native
ConPTY/`PTY()` construction holds the GIL (and/or needs the main thread's COM
apartment / message pump), so moving it to a worker starves the main thread just
the same. **Crucially, no real 30 s-wedge stack was ever captured** — the 136
"in winpty spawn" dumps were all first-chance `0x8001010d` COM exceptions, not
watchdog wedges. The fix was a guess made without the actual fail-path stack.

**Next step (instrumentation kept):** a **soft-stall** capture was added to the
watchdog — when the heartbeat stalls past 3 s (but below the 30 s kill) it dumps
the main-thread stack to boot.log *without* exiting. Reproducing the spawn
freeze with this in place will finally show where the main thread actually
wedges (winpty `PTY()`? QWebEngine pane creation? env/MCP build?), which decides
the real fix. Until then, `pty_session.spawn()` is back to its original form.

---

## Fix attempt #1 — REVERTED (off-thread spawn)

**Primary (real fix):** `PtySession.spawn()` now runs the blocking
`winpty.PtyProcess.spawn()` on a worker `QThread` (`_SpawnWorker`) while a nested
`QEventLoop` keeps the UI painting; the method stays synchronous for callers
(returns with `self._proc` live, or raises — `orchestrator.spawn()` is
unchanged). Off the main thread the COM call is never inside input-synchronous
dispatch, so `RPC_E_CANTCALLOUT` cannot fire, and a slow ConPTY startup can no
longer freeze the UI. Chosen over full-async to avoid rippling a "spawning"
state through every `orchestrator.spawn()` caller. Caveat: the nested event loop
is mildly re-entrant; spawns are serialized by user/orchestrator action.

**Instrumentation (landed alongside):**
1. Watchdog dumps `faulthandler.dump_traceback(all_threads=True)` to boot.log
   right before `os._exit(1)`, so the *next* real 30 s wedge records the exact
   main-thread stack (previously it logged only "wedged Xs").
2. `runtime/boot.log` is rotated (tail kept) once it passes 256 KB — it was
   append-only across 332 boots, and stale "wedged 1s" lines from an old
   watchdog actively misled this investigation.

**Validation:** full suite green (0 failures); new tests `test_pty_session_spawn`
(winpty runs off the main thread; the event loop keeps pumping during a 150 ms
spawn; failures propagate) and `test_watchdog_dumps_main_thread_stack_before_exit`.
**Live confirmation pending a cockpit restart** — the running process still holds
the pre-fix code, so the ultimate proof (spawning panes no longer freezes)
requires relaunching. *(Note 2026-06-04: fix #1 was reverted, so this test file
was removed with it — the reference is kept here only as a record of what the
reverted attempt validated.)*

Relates to improvement-audit cross-cutting root cause **D (main-thread
blocking)**. The 2026-05-29 RCA already moved PTY **reads** off the main thread;
**spawn** was the remaining synchronous main-thread PTY call.
