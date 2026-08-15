# #240 + #232: resource-classifier false positives + restart-reason attribution

## #240 — resource classifier misfires on prohibition text, floods events.log, hides queued panes

### Root cause (1) — substring classifier fires inside negation/prohibition sentences

`resource_governor.py::classify_resource()` scanned raw task text for bare
substrings (`"pip install"`, `"npm install"`, …) with no notion of sentence
context. A task spec written to **forbid** an action —

```
ห้ามรัน `pip install -e .` เด็ดขาด (จะไป repoint venv ของ repo หลัก — บั๊ก #202)
```

— contains the literal substring `"pip install"`, so it was classified
`PACKAGE_INSTALL` exactly like a task that actually runs it. `max_package_install_global`
defaults to 1, so the first such task grabbed the only global slot and every
other task sharing the (mis)classification queued behind it — verified live
against `runtime/events.log` for the wave that triggered this issue
(`resource_slot_acquired` for `backend#1` at 06:51:18, `resource_task_queued`
+ `assign_resource_wait` for `backend#2`/`backend#3` at 06:52:07/06:53:06, no
further admission events for either until the report was filed).

### Root cause (2) — a resource-governor slot is held for the pane's whole task lifetime, not the install step

`assign()` (orchestrator.py) acquires the slot once at dispatch and only
releases it when the pane closes/finalizes (`release_slot` call sites: pane
close, worktree finalize). A pane that never installs anything still holds
the `package_install` slot for its entire task — turning `limit=1` into a
de-facto global serializer for any two tasks that share a (mis)classified
resource class, independent of what either pane is actually doing at any
given moment.

### Root cause (3) — the queue was invisible outside `events.log`

`assign()` answered `ok: task queued (spawning async, +0ms)` for a denied
role, then enqueued it with the governor. Because the role has no pane entry
until admitted, `list_status()`/`list_status_detailed()` (driven purely by
`_project_panes()`) never saw it — the role vanished from `takkub list` /
`takkub status` with no signal it was still alive, waiting. The only way to
find out was reading `runtime/events.log` directly.

### Root cause (4) — `resource_gate_block` still flooded events.log for long waits

#195 already replaced the old "log every 1s tick" behaviour with a
1/2/5/15s backoff schedule for the `dispatch_waiting` retry loop, but never
capped logging for a wait that stays blocked *after* the backoff settles at
its 15s floor — a multi-hour wait still emits one line every 15s forever.
Field evidence: `resource_gate_block` was 1311 of 4049 lines (32%) in one
`events.log`, with 1154 of those attributed to a single pane (`backend#3`).

### Fix

1. **`_marker_signals()`** (resource_governor.py) replaces the bare `any(marker
   in text ...)` scan with a line-by-line scan that skips any line containing
   a negation/prohibition cue (`ห้าม`, `อย่า`, `ไม่ควร`, `ไม่ต้อง`, `don't`,
   `do not`, `never`, `avoid`, `must not`, `shouldn't`, `should not`) before
   checking for a marker on that line. A marker on one line never suppresses
   a marker on another line (multi-line specs with both a prohibition and a
   real instruction still classify correctly — see
   `test_classify_negation_on_one_line_does_not_suppress_marker_on_another`).
   `classify_resource()`'s four marker checks (`BROWSER`/`PACKAGE_INSTALL`/
   `BUILD`/`TEST`) all route through this helper now.

   Considered the alternative the issue floated — classify off commands the
   pane actually runs (via `pane_guard`) instead of the task-spec prompt —
   and did not take it: it's a materially larger change (needs a live
   command-execution feed into the governor, a different data source
   entirely) for the same yield the text-fix already gets, given root causes
   2–4 below still needed addressing regardless of which signal drives
   classification.

2. **Default `max_package_install_global` raised from 1 → 2 for "balanced"
   mode** (`performance_settings.py::preset()`; "maximum" mode was already 2,
   "safe" mode deliberately stays 1 — its whole point is maximum
   conservatism). The classifier fix addresses the root cause of *why* a task
   gets misclassified; this change addresses *how bad it is* when one still
   does — halves the blast radius of any future misclassification without
   materially weakening the guardrail. Did not attempt to bind the slot to
   the actual install span (tracking exactly when a pane starts/stops running
   an install command inside its session) — that needs the same pane_guard
   command-feed groundwork as the classification alternative above and is
   out of scope for this fix.

3. **`_queued_resource_roles()`** (orchestrator.py) mirrors the existing
   `_pending_notice_roles()` pattern (#163: a just-closed-but-still-reporting
   role stays visible in `list_status`/`list_status_detailed` instead of
   vanishing) for governor-queued roles that have no pane yet. Wired into
   both `list_status()` and `list_status_detailed()`. A live pane's real
   state always wins over the synthetic queued entry if both somehow exist.

   `assign()`'s queued reply and the synthetic list entry now share one
   formatter, `_describe_resource_wait()`, which also names the blocking
   pane(s) via the new `ResourceGovernor.holders_for_class()`:

   ```
   backend#2 queued — waiting for package_install slot (package_install_global_limit, blocked by backend#1)
   ```

   `governor.snapshot()` also gained `resource_holders` (`{class: [pane_id, ...]}`)
   for callers that want the same information from a snapshot instead of a
   live `assign()`/`list` call.

4. **Heartbeat throttle for `resource_gate_block`** (resource_governor.py):
   `QueuedTask` gained `last_gate_block_log_at`. The ramp-up attempts
   (covered by `_GATE_RETRY_BACKOFF_S`, i.e. the first 4) still log every
   time — a fresh block is still immediately visible. Once an item is past
   the ramp, `dispatch_waiting` only logs a further `resource_gate_block`
   line once `_GATE_BLOCK_LOG_HEARTBEAT_S` (60s) has elapsed since the last
   one it actually emitted, via a new `request_slot(..., emit_on_deny=...)`
   parameter. The final `resource_gate_unblocked` summary (already added by
   #195) is unaffected — it always fires once, with the true `attempts`/
   `blocked_for_s` regardless of how many intermediate lines were suppressed.

### Test evidence

- `tests/test_resource_governor.py`:
  - `test_classify_ignores_prohibition_sentence_from_real_task_spec` — the
    **verbatim** sentence from this issue's own field report.
  - `test_classify_ignores_prohibition_sentence_from_this_sessions_own_task`
    — a second, independently-worded fixture (this very spawn's task
    boilerplate) proving the fix generalizes, not just fits one string.
  - `test_classify_ignores_english_negation_cues`,
    `test_classify_still_detects_genuine_install_instruction` (positive
    control — the negation filter must not swallow real instructions),
    `test_classify_negation_on_one_line_does_not_suppress_marker_on_another`.
  - `test_gate_block_heartbeat_throttles_long_running_waits` — 90 simulated
    seconds of continuous blocking now emits 4–7 `resource_gate_block` lines
    instead of ~10+ (and would be ~360 without any throttle at a naive 1Hz).
  - `test_holders_for_class_reports_pane_holding_the_slot`.
  - All 7 pre-existing tests (including the #195-pinned backoff-schedule and
    unblock-summary tests) still pass unmodified.
- `tests/test_resource_queue_visibility.py` (new file): `_describe_resource_wait`
  formatting, queued role surfaced in `list_status`/`list_status_detailed`
  with the blocking pane named, live-pane-wins-over-synthetic-entry
  precedence, project-scoping, no-governor no-op.
- `tests/test_performance_settings.py::test_package_install_limit_not_one_in_balanced_or_maximum`.

Run: `PYTHONPATH=src python -m pytest tests/test_resource_governor.py
tests/test_resource_queue_visibility.py tests/test_performance_settings.py -q`
→ 18 + 6 + 7 passed.

---

## #232 — npm-update-triggered restart indistinguishable from a user/CLI restart

### Root cause

`_restart_cockpit()` (update_panel.py) is the single persist+relaunch
implementation shared by every restart path (status-bar button, `takkub
restart`, npm self-update, git-pull self-update, the pip-sync fallback) but
**never logged a `cockpit_restart` event itself** — only two of its callers
did, each with their own hardcoded reason:

- `_on_restart_cockpit_clicked` (button) → `reason="user_action"`
- `Orchestrator.request_restart()` (`takkub restart` CLI) → `reason="cli"`

The npm-update path (`_start_npm_update_install`'s `_installed()` callback)
called `self._restart_cockpit()` directly with **no** `cockpit_restart`
logging of its own — nor did the git-pull-update path or the pip-sync
fallback. `events.log` gave no way to tell "the cockpit restarted itself to
apply an update" from "nothing was logged for this restart at all", which
in practice reads the same as the issue's underlying complaint: when panes
get restored + re-sent their last task after an unattended restart, there
was no way for Lead (or the user) to tell *why* it happened without digging
into `boot.log`.

### Fix

- `_restart_cockpit()` gained an opt-in `reason: str | None = None, **extra`
  parameter. Left `None` (its default) it's a pure no-op for logging — the
  two callers that already log their own reason before calling it keep doing
  exactly that, so nothing double-logs. Callers with no reason of their own
  now pass one explicitly:
  - `_installed()` (npm self-update) → `reason="npm_update", version=<latest>`
  - `_on_pull_update_done`'s non-deps-changing path (git-pull self-update) →
    `reason="git_pull_update"`
  - `_restart_with_pip_sync`'s detached-spawn-failed fallback →
    `reason="pip_sync_fallback"`
- A small marker file (`runtime/restart-reason.json`, written via
  `_write_restart_reason_marker()`) carries the reason across the process
  boundary a restart performs — a `_log_event` line alone doesn't survive
  that, and the reason needs to reach the *successor* process's
  `restore_teammates()` to annotate the Lead-facing restore notice. Every
  restart path now writes it (including the two that already self-logged —
  `_on_restart_cockpit_clicked` and `request_restart()` — so the restore
  notice covers all five reasons, not just the three new ones).
- `restore_teammates()` reads-and-clears the marker once at boot
  (`_read_and_clear_restart_reason()` — single-use, so a later organic
  restart with no fresh marker never repeats a stale reason) and appends a
  suffix via `_restart_reason_suffix()` to every `[cockpit restart] <role>
  pane restored…` notice body, e.g.:

  ```
  [cockpit restart — restarted to apply update v1.2.3] backend pane restored from last session and last task re-sent automatically.
  ```

### Test evidence

- `tests/test_restart_cockpit.py::TestRestartCockpitReasonThreading` —
  `reason=None` logs nothing (no double-log regression), `reason="npm_update"`
  logs `cockpit_restart` + writes the marker with the right payload. All 19
  pre-existing tests in this file (audit-event, delegation, cancel,
  port-file-release, port-file-provenance) pass unmodified.
- `tests/test_session_resume.py` — `test_restore_notice_carries_npm_update_reason`
  (marker present → suffix appended, marker consumed/deleted after read),
  `test_restore_notice_has_no_reason_suffix_when_marker_absent` (no marker →
  notice body unchanged from before this fix, byte-for-byte).
- `tests/test_worktree_assign.py::TestRequestRestart::test_writes_restart_reason_marker_for_restore_notice`
  — the CLI path writes `reason="cli"` to the marker.

Run: `PYTHONPATH=src python -m pytest tests/test_restart_cockpit.py
tests/test_session_resume.py tests/test_worktree_assign.py -q` → 21 + 9 + 24
passed.

---

## Verification run (both issues combined)

```
PYTHONPATH=src python -m pytest \
  tests/test_resource_governor.py tests/test_resource_queue_visibility.py \
  tests/test_performance_settings.py tests/test_restart_cockpit.py \
  tests/test_session_resume.py tests/test_worktree_assign.py \
  tests/test_pending_done_notice_visibility.py tests/test_project_scoping.py \
  tests/test_orchestrator_shard.py -q
```
→ all passed (three consecutive full runs, no flakes attributable to this
change — one transient `FileNotFoundError` in `test_project_scoping.py`
during an earlier combined run did not reproduce on retry with or without
this diff applied, isolating it as pre-existing environmental flakiness, not
a regression from this change).

`ruff check` / `ruff format --check` clean on every touched file.
`lint-imports`: 24/24 contracts kept.

## Not done / follow-ups

- Root cause 2 (slot held for the pane's whole task lifetime) is mitigated
  (limit 1→2) but not eliminated — binding the slot to the actual install
  command's span needs a pane_guard → resource_governor command-execution
  feed, which is a materially bigger change than this fix's scope.
- `_queued_resource_roles()` reasons stay in the coarse form the governor
  already produces (`package_install_global_limit`, `cpu_high`, …) — no
  further wordsmithing beyond naming the blocking pane(s).
