# main_thread_stall (#194), resource_gate_block flood (#195), self-reference false positive (#199)

2026-08-14 · backend

## #194 — main_thread_stall from `_check_shell_open_dialog` / `_read_tail_bytes`

**Before:** `Orchestrator._check_shell_open_dialog` ran on **every** 5s idle-watchdog
tick (`_check_idle_teammates` → `_check_stuck_panes`) for **every** `working` pane,
opening + reading up to 64 KiB of the pane's transcript file on the Qt main thread
with no throttle. events.log showed 8 `main_thread_stall` events (860ms–2516ms) in
~12 minutes, all with `spawn_in_progress=false, active_heavy_tasks=0` — i.e. the idle
path, not a spawn/heavy-task burst. boot.log's watchdog stack dumps pointed straight
at `orchestrator_text.py:185 _read_tail_bytes` under this call chain. A bounded 64 KiB
read shouldn't normally cost 900ms–2.5s; the leading suspect is Windows Defender
real-time scanning a fresh `open()` handle, which throttling (not shrinking the byte
cap further) is the correct lever against.

**Fix (`orchestrator.py`, `spawn_engine.py`):**
- `_SHELL_DIALOG_SCAN_INTERVAL_S = 30.0` — per-pane throttle (`PaneState.last_dialog_scan_ts`),
  cuts scan frequency from every 5s tick to at most once per 30s.
- The actual `open()`+`read()`+decode now runs on a daemon background thread
  (`PaneState.dialog_scan_in_flight` coalesces so a slow scan can't pile up threads);
  the eventual `_notify_lead` call is marshalled back to the GUI thread via
  `QTimer.singleShot(0, ...)`, matching the existing `_hot_md_worker` precedent.
- Investigated the second boot.log hotspot (`update_panel.py:622 _installed`) and traced
  it to `_restart_cockpit()` calling `write_session_snapshot()` + `write_resume_briefs()`
  right before `QCoreApplication.quit()` — which then triggers `MainWindow.closeEvent`,
  which calls **both again** moments later. `write_resume_briefs()` scans every open
  project's chatlogs (`build_resume_brief`) and is the expensive half of that pair.
  Added `_RESUME_BRIEF_MIN_INTERVAL_S = 10.0` throttle so the closeEvent's redundant
  second pass is a no-op instead of a second multi-second GUI-thread stall.
- Regression tests: `tests/test_stuck_recover.py::TestShellOpenDialogScanThrottle`
  (no rescan within throttle window, rescans after it elapses, notify marshalled via
  `QTimer.singleShot`, in-flight flag clears) and
  `tests/test_path_traversal_vault.py::TestWriteResumeBriefsThrottle` (second call
  within interval is a no-op / scans again once interval elapses).

## #195 — resource_gate_block log flood

**Before:** `Orchestrator._resource_timer` ticks `_tick_resource_governor()` every 1s,
which calls `ResourceGovernor.dispatch_waiting()`. For a queued task that stays denied,
`dispatch_waiting` called `request_slot()` **unconditionally** every tick, and every
denial unconditionally emitted `resource_gate_block` — proven as 15 identical lines in
15s for one task (`heavy_project_limit`, project `saas_admin`) before `cockpit_restart`
cut it off. No backoff, no dedupe, no unblock summary.

**Fix (`resource_governor.py`):**
- `_GATE_RETRY_BACKOFF_S = (1.0, 2.0, 5.0, 15.0)` — `QueuedTask` gained `attempts` /
  `next_retry_at` / `reason`. `dispatch_waiting` now skips `request_slot` entirely
  (no log line, no lock churn) for a queue head whose `next_retry_at` hasn't elapsed —
  attempts land at t=1, 2, 4, 9 (4 lines) instead of every second (10 lines) over a 10s
  window. `next_retry_at` starts at 0.0 (immediately eligible) so a freshly freed slot
  is still grabbed on the very next tick — verified this doesn't regress the existing
  `test_waiting_queue_is_round_robin_by_project` fairness test.
- On eventual admission, `dispatch_waiting` emits one `resource_gate_unblocked` summary
  (`blocked_for_s`, `attempts`) instead of relying on the per-attempt flood for context.
- `snapshot()`'s `waiting_tasks` now carries `reason` + `attempts` per queued item;
  `assign()` passes the initial denial `reason` through to `enqueue()`.
- `status_header.py`'s performance chip tooltip/dialog now appends
  `(waiting: <reasons>)` next to the resource-queue count, sourced from that same
  `waiting_tasks` reason field — satisfies "show *why* work is waiting" without new UI.
- Regression tests: `tests/test_resource_governor.py` —
  `test_gate_block_backoff_reduces_retry_frequency`,
  `test_gate_unblock_emits_single_summary_event`,
  `test_freed_slot_admits_immediately_without_backoff_delay`,
  `test_waiting_tasks_snapshot_exposes_reason`.

## #199 — tripwire false-positived on its own source (found by Lead mid-fix)

**Real incident:** while fixing #194 above (editing `orchestrator.py` around line 4433,
which is the `_check_shell_open_dialog` f-string that quotes the marker text), this
pane's own transcript accumulated the marker string 9 times — purely from Read/Edit
tool echoes of the source line itself. The cockpit fired the #104 tripwire against a
pane that was demonstrably not stuck (`takkub status`: last progress 0s ago) and with
zero `OpenWith.exe`/`AppPicker`/`rundll32` process on the machine.

**Fix (`orchestrator.py`, `orchestrator_text.py`):**
- **Idle gate** (`_SHELL_DIALOG_IDLE_GATE_S = 20.0`): the scan is skipped entirely
  unless `pane._last_output_ts` is at least this stale — a real modal dialog freezes
  the process, so an actively-progressing pane structurally cannot be blocked on one.
  Doubles as a #194 win: an active pane skips the file open, not just gets throttled.
  (Bug caught during implementation: the check originally called `time.time()`
  internally instead of using the watchdog tick's `now`, which made it untestable and,
  worse, inconsistent with every other `now`-based comparison in the same tick —
  `_check_shell_open_dialog` now takes `now: float` as an explicit parameter.)
- **Self-reference filter** (`_looks_like_source_reference`, pure function in
  `orchestrator_text.py`): a matching line is discarded if it reads like source/diff
  context — Read-tool `cat -n` line-number prefix, a `+`/`-`/`@@` diff marker, any
  quote character (covers the f-string case that caused this incident), or the literal
  constant name `_SHELL_OPEN_DIALOG_MARKER`.
- **Process corroboration** (`_open_with_dialog_process_present`, Windows-only,
  `sys.platform == "win32"`-gated): even a non-self-reference candidate line no longer
  notifies unless `OpenWith.exe`/`AppPicker.exe`, or `rundll32.exe` running
  `shell32.dll,OpenAs_RunDLL`, is actually present via `psutil.process_iter`. On any
  other platform the notify path is unreachable — this dialog type has no macOS
  equivalent, so the "other branch" is simply that the tripwire never fires there.
- Regression tests added to `tests/test_stuck_recover.py::TestShellOpenDialogTripwire`:
  `test_active_progress_suppresses_notification` (the literal incident — marker present,
  pane progressing → no notify), `test_no_corroborating_process_suppresses_notification`,
  `test_self_reference_line_suppresses_notification` (quoted f-string line → no notify),
  `test_non_windows_never_notifies`. All prior "true positive" tests updated to also
  mock the corroborating process + require idle silence, matching the new two-gate
  contract.

## Test evidence

```
tests/test_stuck_recover.py ................................ (40 passed)
tests/test_resource_governor.py ......... (9 passed)
tests/test_path_traversal_vault.py ....... (9 passed)
tests/test_throughput_watchdog.py / test_lifecycle_recovery.py / test_update_splash_recovery.py — all passing (signature-only touch: `_check_shell_open_dialog` stubs gained the new `now` param)
tests/test_performance_health_chip.py — 1 passed
tests/test_close_event_remote_stop.py — 3 passed
ruff check — all clean on every touched file
```

Run via: `PYTHONPATH="$(pwd)/src" <repo-root>/.venv/Scripts/python.exe -m pytest ...`
(worktree has no own `.venv`; editable install resolves to the ROOT checkout's
`agent_takkub` unless `PYTHONPATH` points at the worktree's own `src/` first — see
`runtime/role-memory/agent-takkub/backend.md`).

Full suite intentionally NOT run here — reserved for the qa batch gate per project policy.
