# #364 lever 1 spike — discard renderer of hidden panes

Spike only (not implementation). Script: `tools/spike_pane_discard_ram.py` · repeatable test:
`tests/test_pane_discard_spike.py` (run via `takkub qa-gate --targeted tests/test_pane_discard_spike.py`).

## Method

`tools/spike_pane_discard_ram.py` boots a real `QTabWidget` of real `TerminalWidget` (xterm.js /
`QWebEngineView`) instances under `QT_QPA_PLATFORM=offscreen`, with the same
`QTWEBENGINE_CHROMIUM_FLAGS` production sets in `app.py` (including
`--renderer-process-limit=4`). It writes sample ANSI content to every pane, marks all but tab 0
hidden via `set_keepalive(False)` (the exact call `ProjectTab._apply_pane_keepalive` makes today),
samples RSS of every child `QtWebEngineProcess`, calls
`QWebEnginePage.setLifecycleState(Discarded)` on the hidden panes, samples RSS again, then switches
back to each hidden pane and times reload + repaint + content fidelity. Repeated 2x for stability.

**Side-finding while building this**: both `tests/test_terminal_widget.py` and
`tests/test_editor_widget.py` document "constructing a real `QWebEngineView` hard-aborts pytest even
under offscreen." Root cause found here: their `qapp` fixtures use `QApplication([])` — an empty
argv leaves Chromium's `base::CommandLine` with no argv[0], and the instant a real
`QWebEngineView` spins up its renderer process, the whole process hard-aborts natively (Windows exit
-1073740791 / 0xC0000409), no Python exception. Swapping to `QApplication(sys.argv)` made the abort
disappear (repro'd directly). Not fixed here — separate, more careful piece of work since pytest's
own argv/plugins aren't guaranteed as clean as this script's — flagging for whoever picks up real
WebEngine test coverage next.

## Results

### 4 panes (Lead + 3 teammates — today's policy ceiling, `pane ≤ 3` + Lead = 4, exactly at
`--renderer-process-limit=4`), 3 hidden, 2 runs:

| | run 1 | run 2 |
|---|---|---|
| RSS before (4 renderer processes) | 280.1 MB | ~283 MB |
| RSS after discarding 3 hidden | 83.3 MB (1 process left) | ~86 MB (1 process left) |
| **freed total** | 196.8 MB | 196.3 MB |
| **freed / hidden pane** | **65.6 MB** | **65.4 MB** |
| reattach total (reload+repaint) per pane | 391 / 375 / 327 ms | 376 / 359 / 374 ms |

Renderer **process count drops from 4 to 1**, not just heap-empties within a process — Discarded
fully tears the process down on this machine/Qt version (PyQt6-WebEngine 6.8.0).

### 6 panes, 5 hidden (exceeds the process-sharing cap):

| | value |
|---|---|
| RSS before (Chromium already shares: only 4 processes exist for 6 pages) | 285.0 MB |
| RSS after discard | 88.1 MB (1 process) |
| freed total | 196.9 MB |
| **freed / hidden pane** | **39.4 MB** ⚠️ below the 60 MB bar |

Total RAM freed is roughly constant (~197 MB — the same ~3 processes' worth), but it's now divided
among 5 hidden panes instead of 3, so **the per-pane number is not a constant — it depends on how
many panes are sharing a renderer process, which depends on total pane count vs
`--renderer-process-limit=4`.**

### Frozen (contrast, not needed — Discarded worked fine):

`LifecycleState.Frozen` is confirmed **not** useful for this goal: process count stays at 4, RAM
freed ≈ 0 MB (−0.2 MB, noise). Matches Chromium's own semantics — Frozen suspends JS timers but
keeps everything resident; only Discarded evicts the renderer. No fallback needed since Discarded
already works.

## Answers to the 4 questions

1. **RAM returned**: ✅ **65.4–65.6 MB/pane** at today's actual pane ceiling (4 total panes) — clears
   the master plan's ≥60 MB/pane bar. Falls to ~39 MB/pane once total panes exceed the
   `--renderer-process-limit=4` cap — **re-measure before raising that pane-count policy**, don't
   assume the number holds. Frozen gives ~0, use Discarded.
2. **Re-attach cost + fidelity**: ~300–390 ms total (reload ~140–205 ms + repaint ~170–190 ms).
   Text content written *after* reattach matches correctly. **Scrollback is lost** on every
   reattach (`scrollback_lost: true` in every run) — xterm.js's buffer is gone with the discarded
   page, and `PtySession.screen` is a plain `pyte.Screen(cols, rows)`, **not** `pyte.HistoryScreen`
   — it never held scrollback either, only the current visible grid. So "we already have the
   source of truth, don't need the old DOM" (the issue's framing) is **half right**: pyte *can*
   reconstruct the current visible screen (it tracks per-cell fg/bg/bold via `pyte.Char`, not just
   text — `_safe_screen_display` in `pty_session.py` already walks this for exports), but it
   **cannot** restore scrollback history above that. Color/attribute fidelity of the reconstructed
   current screen was not verified in this spike (would need a JS-side per-cell style compare,
   outside spike scope) — flag as an open item for implementation, not a go/no-go blocker.
3. **Doesn't collide with ready-marker / IPC / delivery-verify**: confirmed both statically and by
   construction. `pty_session.py` (ready-marker classification, idle detection) and
   `task_delivery.py` (delivery verification) have **zero imports** of
   `PyQt6.QtWebEngineWidgets` or `terminal_widget` — grepped directly, not assumed. They classify
   state from raw PTY bytes / a headless `pyte.Screen`, decoupled from whatever lifecycle state the
   `QWebEngineView` is in (per `docs/ARCHITECTURE.md`'s own note: "pyte is a headless state model
   now... not the render path"). This spike's script never touches `PtySession` at all and nothing
   broke — proof by absence of interaction, not just code reading.
4. **Windows / macOS**: no platform branching needed. `LifecycleState`/`setLifecycleState` is a
   plain `QWebEnginePage` API (Qt/Chromium, not OS-specific), and the `--renderer-process-limit`
   flag is a Chromium flag applied unconditionally in `app.py` today (not gated by `sys.platform`).
   This lever lives entirely in `TerminalWidget`, one layer above the OS-specific PTY backends
   (`_pty_backend.py`'s ConPTY vs POSIX split) — it never touches that code. Windows numbers above
   are measured directly; macOS is expected to behave identically (same Chromium engine, same Qt
   API) but wasn't spot-checked on this machine.

## Go / no-go

**GO**, scoped to the current policy ceiling (≤3 teammates + Lead = 4 concurrent panes). 65 MB/pane
clears the ≥60 MB/pane bar from the master plan, reattach is sub-400ms, and it doesn't touch any of
the systems the issue worried about. The one real cost is scrollback loss on reattach, which is a
UX regression (not a correctness one) worth deciding on explicitly, not silently.

## If go: implementation sketch (not built — next task)

**Hook point**: `TerminalWidget.set_keepalive(active: bool)` in `terminal_widget.py` — the single
place every hidden/visible transition already flows through (`ProjectTab._apply_pane_keepalive` →
`AgentPane.set_keepalive` → here). No changes needed in `ProjectTab`/`AgentPane` at all.

- **Debounce, don't discard instantly**: `active=False` should start a single-shot `QTimer` (e.g.
  20–30s) before calling `setLifecycleState(Discarded)`, not discard on every tab flip — a user
  glancing at another pane for 2 seconds shouldn't pay a ~350ms reload the moment they glance back.
  `active=True` cancels the pending timer; if already discarded, drives the reattach sequence.
- **Reattach sequence**: reset the ready-gate (mirrors `_page_ready = False` in this spike) →
  `setLifecycleState(Active)` → wait for the existing `pageReady` bridge signal (already fires
  correctly on reload — confirmed in this spike, `QWebChannel` object registration survives
  discard/undiscard) → replay a snapshot.
- **Snapshot strategy (v1, cheap)**: call the *existing* `request_buffer_text()` /
  `termGetBufferText()` right before discarding, hold the plain text, replay it via `write_bytes()`
  once reattached. Restores scrollback continuity but loses color/attributes (plain text only) —
  an explicit, visible tradeoff, not a silent bug. A v2 that reconstructs SGR-accurate output from
  `PtySession.screen`'s per-cell `pyte.Char` attributes would fix the *current visible screen's*
  colors but still can't recover scrollback (pyte never held it) — worth doing only if users
  actually complain about the plain-text look of restored scrollback.
- **Guard `write_bytes`/`_flush_writes` against a currently-discarded page**: confirmed in this
  spike that `runJavaScript` on a `Discarded` page's `QWebEnginePage` just silently no-ops /
  returns `None` — extend the existing `_page_ready` gate (which already buffers into
  `_pending_writes` before the page is ready) to also treat "discarded" as "not ready," so PTY
  bytes arriving while suspended aren't dropped, they queue and flush into the reconstructed
  snapshot on reattach.
- **Never discard the current tab**: `ProjectTab._apply_pane_keepalive` already guarantees
  `set_keepalive(True)` only for `w is cur`; the debounce timer must be cancelled the instant
  `active=True` arrives so a fast tab-back never races a discard that was already in flight.
- **Teardown**: `destroy_terminal()` must cancel the pending discard timer alongside the existing
  `_flush_timer`/`_heartbeat` stop — same reasoning (no runJavaScript into a half-destroyed view).
- **Re-verify the 60MB/pane number if the pane-count policy ever changes** — this spike showed it
  drops to ~39MB/pane past the current `--renderer-process-limit=4` boundary.
