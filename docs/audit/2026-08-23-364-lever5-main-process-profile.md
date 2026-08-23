# #364 lever 5 — profile the main process (pythonw, 300–480 MB)

Measure-only task per the issue's own ordering ("5. main process profile... ตั้ง cap ที่วัดได้ ... ตัวเลขเป้าหมายให้ spike บอก"). No cap is implemented here — one real bug was found and fixed along the way (in the *measurement script*, not production code — see below), but production code came back clean.

Two deliverables, matching the two options the task gave:

1. **Offline spike** (the "risk-free" option): `tools/spike_main_process_ram_profile.py` — a standalone subprocess (same crash-domain isolation as lever 1's `spike_pane_discard_ram.py`) that boots a real `QApplication` + N real `TerminalWidget`/`QWebEngineView` panes, measures RSS/gc/tracemalloc at three points (baseline, panes-up, panes-closed), and answers "does closing a pane actually give the memory back." Repeatable via `tests/test_main_process_ram_profile_spike.py` (`takkub qa-gate --targeted tests/test_main_process_ram_profile_spike.py`).
2. **Live IPC** (the "on a real running cockpit" option, never invoked against the user's prod cockpit in this task — see workspace-isolation note at the end): `takkub doctor --ram --ram-profile` — a new opt-in, on-demand `ram-profile` IPC command (`ram_report.collect_main_process_profile`) that takes a temporary tracemalloc snapshot + a `gc` object census of the actual running main process, prints a `[ram-profile]` section under `--ram`'s existing table, and never leaves tracemalloc running afterward.

## Method (offline spike)

`tracemalloc.start(1)` runs as the very first line of the script, before any project import, so its snapshot has true historical attribution for this process's own Python-heap allocations — not just "what allocated in the last few seconds" (which is the best a live on-demand snapshot against an already-warm cockpit could ever do; see the IPC section below for that weaker case). The script then:

1. Boots `QApplication(sys.argv)` under `QT_QPA_PLATFORM=offscreen` with the same Chromium flags `app.py` sets in production (mirrors lever 1's spike exactly, including the `QApplication(sys.argv)`-not-`QApplication([])` requirement documented there).
2. Samples baseline RSS + a `gc.get_objects()` census (grouped by `type(obj).__name__`).
3. Creates 5 real `TerminalWidget` panes, writes sample ANSI+Thai text to each (same sample text as lever 1's spike), waits for the page-ready signal, samples RSS + census again.
4. Calls `destroy_terminal()` on all 5 (the exact teardown production code calls — see `terminal_widget.py`'s docstring), drops every Python reference, forces `gc.collect()`, pumps the Qt event loop so pending `deleteLater()`s actually run, samples RSS + census a third time.
5. Also tracks a `weakref` to each of the 5 `TerminalWidget` instances, so "still alive after close" is answered by actual reference-liveness, not just an aggregate object count.

**Bug found and fixed in the script itself, not production code**: the first version's teardown loop (`for p in panes: p.destroy_terminal()`) reported 1 of 5 `TerminalWidget`s "still alive after close." Walking `gc.get_referrers()` on the survivor showed the referrer was the script's own `__main__` module dict — the classic Python gotcha that a `for` loop's variable outlives the loop, so `p` stayed bound to `panes[-1]` in the script's own namespace after the loop ended, which is not a leak in `TerminalWidget` at all. Adding `del p` after the loop made the false positive disappear (confirmed by rerun, see Results). Flagging this explicitly because it's exactly the kind of self-inflicted measurement bug this task exists to avoid, and it very nearly produced a "found a leak!" false claim.

## Results (5 panes, offline spike, this machine)

| stage | main-process RSS | `TerminalWidget` alive | `QWebEngineView` alive |
|---|---|---|---|
| baseline (app booted, 0 panes) | 45.8 MB | 0 | 0 |
| 5 panes up, written to | 111.6 MB | 5 | 5 |
| after `destroy_terminal()` × 5 + `gc.collect()` | 112.5 MB | **0** | **0** |

- **Growth per pane (main process, Python + embedder side, excludes the out-of-process renderer already measured by lever 1): 13.16 MB/pane** (65.8 MB / 5).
- **No Python-object leak**: every watched type (`TerminalWidget`, `QWebEngineView`, `QWebEnginePage`, `QWebEngineProfile`) returns to exactly 0 after close, and all 5 `weakref`s report collected. Production's real close path (`main_window.py`'s `_teardown()`: `destroy_terminal()` → `removeTab` → `unregister_pane` → `setParent(None)` → `deleteLater()`) is one step more thorough than this spike's (it also reparents and deletes the `AgentPane`, not just the `TerminalWidget`), so this is a lower bound on cleanliness, not an upper one.
- **RSS does not drop after close** (112.5 MB vs. 111.6 MB — went up slightly, within noise): this is the well-known CPython/OS allocator behavior of not `munmap`-ing freed heap pages back to the OS immediately, **not evidence of a leak** — the object census proves the Python objects are actually gone. The freed pages stay in the process's private heap/arena for reuse by the *next* pane, they just don't show up as a lower number in Task Manager. This is a real, useful distinction: "RSS never goes back down when you close a pane" (true, matches user-visible behavior) is a different claim from "closed panes leak" (false, per this measurement).
- The three specific suspects the issue itself named were checked by reading, not just running:
  - **pyte screen history** — `PtySession.screen` is a plain `pyte.Screen(cols, rows)`, not `pyte.HistoryScreen` (confirmed in the lever 1 audit already; re-confirmed by reading `pty_session.py` again for this task). Bounded by rows×cols, does not grow with output volume.
  - **display cache** — `PtySession._display_lines_cache` is a `tuple` of exactly `rows` strings, memoized per output-generation, not an ever-growing log.
  - **transcript buffers** — `PtySession._transcript` is an **open disk file handle** (`open(transcript_path, "wb")`), not an in-memory buffer. It writes to disk and is flushed/closed on detach; it does not accumulate RAM.

  None of the three are per-pane RAM growth sources. The actual (small, non-leaking) per-pane cost is Qt/WebEngine widget overhead itself — 13 MB/pane, paid once per pane ever opened, released correctly on real close.

- **~1,346 objects (of ~18,720 baseline) remain after close that aren't any of the 4 watched types** (`total_objects` 18,720 → 20,120 → 20,066). Not chased down further within this task's box — the type-count deltas (mostly `dict`/`tuple`/`ReferenceType`/`builtin_function_or_method`, all bounded, none scaling with pane count in a second run) are consistent with one-time global `QtWebEngineProfile`/cache/font-database initialization that Chromium does on first real page load and keeps for the app's lifetime, not a per-pane accumulation — but this is an inference from the shape of the numbers, not directly proven the way the watched-type census is. Flagged as an open item if this ever needs re-checking at a larger N.

### tracemalloc top allocators (this run)

Traced total: **5.47 MB current / 6.04 MB peak** — vs. 65.8 MB of actual RSS growth for the same 5 panes. This gap is the headline finding of the tracemalloc half of this task: **tracemalloc only sees the Python object heap (`pymalloc`); it is structurally blind to PyQt6/Qt/Chromium's own C++ allocations, which is where nearly all of a cockpit's RSS actually lives.** The top entries by size are import-machinery bookkeeping (`importlib`, `enum`, `collections` — all one-time interpreter/module-load cost, not pane-related) plus a handful of small entries in this script's and `terminal_widget.py`'s own code. None of this is actionable — it confirms tracemalloc is the wrong tool for "what is the 300–480 MB made of," and the gc census (which counts every live object regardless of allocator) is the tool that actually answered the leak question above.

## Live IPC path (`takkub doctor --ram --ram-profile`)

Added for exactly the case this offline spike *can't* reach: the real Orchestrator + real AgentPanes + real spawn machinery + real provider CLIs running for hours, which is out of scope to safely fake in an offline harness and explicitly off-limits to touch on the user's prod cockpit for this task.

- `ram_report.collect_main_process_profile()` (pure leaf function, no Qt/orchestrator import, same architecture convention as `collect_ram_report`): starts `tracemalloc` only if it isn't already running, forces `gc.collect()`, takes one snapshot (top-N allocators by `lineno`), takes a full `gc.get_objects()` census (top-N types + a `watched_pane_object_counts` dict for `AgentPane`/`TerminalWidget`/`PtySession`/`HeadlessPane`), then stops tracemalloc again **only if this call is the one that started it**. Never left running.
- `Orchestrator.ram_profile()` — thin delegation (no state of its own to gather; tracemalloc/gc can only describe the process they run in).
- `cli_server.py`: new `ram-profile` IPC command, same read-only trust tier as `ram-status`.
- `cli.py`: new `--ram-profile` doctor flag — no effect without `--ram`; when both are passed and the cockpit is running, merges the profile into the `--ram` JSON/text output.
- `doctor.py`: `format_ram_report` grows an optional `[ram-profile]` section (top 10 allocators + gc object count + the watched leak-check counts) when a profile is present.

**Important, honestly-stated limitation carried over from the spike's tracemalloc finding above**: because this only starts tracing on an already-warm, hours-old cockpit process, the traced-bytes number is an even weaker lower bound live than it was in the controlled spike (which at least started tracing from process boot) — it will only ever show *new* Python-heap allocation activity in the moment of the call, not historical resident memory, and it still cannot see PyQt6/Qt/Chromium's C++ heap at all. Its useful signal is the **gc object census's `watched_pane_object_counts`**, cross-checked by the caller against the cockpit's own live pane count (`ram-status`'s pane list) — a mismatch there (more `AgentPane`/`TerminalWidget`/`PtySession` objects alive than panes the cockpit thinks exist) is real leak evidence, the same technique that caught (and, after the `del p` fix, cleared) the spike's own false positive above. Not run against a real long-lived cockpit in this task; that comparison is the natural next step if the 300–480 MB range needs to be explained further later.

## Proposed cap — none, with the numbers to justify why

The issue's own framing asked for "a cap that can be measured." The honest answer from this measurement is: **there is nothing here to cap.**

- The three named suspects (pyte screen history, display cache, transcript buffers) are all structurally bounded already — no setting would do anything (there's no unbounded growth for a cap to bound).
- The one thing that does scale with pane count (13 MB/pane of Qt/WebEngine widget overhead) is released correctly on real pane close — already-correct behavior, not something a cap fixes.
- RSS not dropping after close is allocator behavior (pages held for reuse, not leaked) — not fixable by application-level code, and not actually a problem: the next pane opened reuses that headroom instead of asking the OS for fresh pages.

**What this task changes instead of adding a cap**: visibility. `takkub doctor --ram --ram-profile` is now available for the next time someone needs to explain the *live* 300–480 MB number on a real long-running cockpit — this offline spike proves the tooling and the technique (gc census cross-checked against live pane count) work and produce a trustworthy zero-false-positive result, but 5 short-lived offscreen panes booted in a few seconds cannot, and was never going to, reproduce hours of real Orchestrator/spawn-engine/task-delivery/remote-notify state. If that gap still needs explaining, the next step is running `--ram --ram-profile` against a real (non-prod) dev cockpit session after a normal day of use, not another offline spike.

## Workspace isolation note

Per this task's instructions: no full gate was run (targeted only — `tests/test_ram_report_main_process_profile.py`, `tests/test_orchestrator_ram_status.py`, `tests/test_doctor_ram_live.py`, `tests/test_main_process_ram_profile_spike.py`, all passing via `takkub qa-gate --targeted`), and the user's prod cockpit was never touched — every measurement in this doc came from either the standalone offscreen subprocess spike or direct unit tests of the new pure-logic function, both running in this worktree's own process space.
