# #4/#107 debug: auto `/remote-control` did not fire on fresh Lead boot

## Repro discipline followed

1. Read live evidence first: `runtime/events.log` 13:31–13:38 on 2026-07-09 —
   two fresh Lead spawns (13:32:03, 13:34:42), both `resumed: false`, neither
   followed by an `auto_slash_command` event for `/remote-control` at any
   point afterward. Only `/resume` (button-triggered) shows up, and it fires
   **twice** at 13:37:38.
2. Read the code path end to end (`spawn_engine.py` `spawn()` →
   `_maybe_fire_remote_bridge` → `lead_inbox.py`
   `inject_slash_command_when_ready`) before touching anything.
3. Wrote real-timing repro tests (`tests/test_remote_bridge_repro.py`) that
   drive the actual `QTimer.singleShot` poll chain with a controllable fake
   session — **not** the pre-existing `test_remote_bridge_autofire.py`
   pattern, which mocks `inject_slash_command_when_ready` out entirely and
   therefore could never have caught this class of bug (confirmed: those
   tests were green while the bug shipped — "fake-vs-real drift").

## Root causes found (proven by code read + repro, not guessed)

### A. Every drop path in `inject_slash_command_when_ready` was silent (confirmed bug, now fixed)

`_check()`'s poll loop had exactly three ways to end without delivering:
pane missing, session dies mid-poll, and the 45s `max_wait_ms` timeout. None
of them logged anything — `return` with no trace. This is why the incident
is invisible in events.log: **if** the real Lead pane took longer than 45s
to reach its ready prompt during the observed boot (cold claude start +
boot-storm main-thread stalls + the auto-trust-modal poll all compete for
the same window), the bridge would have been silently dropped and we would
see exactly the log gap that was observed. Proven with
`test_never_ready_within_window_drops_with_timeout_reason` (repro exercises
the drop) and `test_session_dies_mid_poll_drops_with_reason`.

**Not fully provable which exact exit path fired on 2026-07-09** (the log
had zero signal for any of them, by design of the bug) — this is the
precise gap observability closes. Going forward every drop logs
`auto_slash_command_dropped` with a `reason` (`pane_missing`,
`session_dead`, `timeout_not_ready`, `timeout_draft_blocked`).

### B. `consume_session_report` never re-fired the bridge (confirmed by code read — this is a real, separate gap, item 4 in the task spec)

`_maybe_fire_remote_bridge` is only called from two places pre-fix: the
`spawn()` success path. It was **never** called from
`Orchestrator.consume_session_report` (the `SessionStart` hook consumer that
fires on every startup/resume/clear/compact and is the *only* place that
learns about a session-uuid change caused by a manual `/resume` typed
inside the Lead pane — see its own docstring). So: user boots Lead, bridge
either fires or silently drops (per A); user later runs `/resume` inside the
pane, claude switches to a brand-new transcript uuid, and `/remote-control`
is never (re-)established for that session — with zero code path that would
ever fire it. This matches the observed transcript: "Lead transcript
lead-133442 first /remote-control mention is Lead's own summary prose
post-/resume, not an injection."

Fixed by having `consume_session_report` call `_maybe_fire_remote_bridge`
for the Lead role on every report. Safe for the common "startup" source
(same uuid `spawn()` already fired/is polling for → dedup no-ops) and closes
the gap for resume/clear/compact (new uuid → fires again). Proven by
`TestSessionReportRefiresBridgeForNewSession`.

## Fix

1. **Observability** (`lead_inbox.py`): `inject_slash_command_when_ready`
   gained `on_delivered`/`on_dropped(reason)` callbacks and logs
   `auto_slash_command_dropped` with a reason on every exit path that isn't
   a successful paste.
2. **Longer + retryable window for the bridge specifically**
   (`spawn_engine.py`): the bridge now waits `_REMOTE_BRIDGE_MAX_WAIT_MS =
   90_000` (matches the existing gemini/codex slow-boot precedent in
   `_ready_wait_ms`) instead of the generic 45s used for one-off slash
   injections. On top of that, a session that still drops (timeout/session-
   dead) is **not** marked "handled" — it's retried by the idle-watchdog
   tick (`_check_idle_teammates` → new `_reap_remote_bridge`, 5s cadence)
   the next time that Lead pane is observed at its ready prompt, so a
   pathological boot (>90s) still eventually gets the bridge instead of
   losing it for the rest of the session. `_lead_remote_bridge_pending`
   (new set, mirrors `_lead_remote_bridge_fired`) prevents the spawn-time
   call and the reaper from double-polling the same session concurrently.
3. **`/resume` (and `/clear`, post-compact) re-fire** (`orchestrator.py`):
   `consume_session_report` now calls `_maybe_fire_remote_bridge` for the
   Lead role on every `SessionStart` report, closing gap B above.

## Bonus (point 5 of the task spec) — reported, not fixed here

Duplicate delivery observed at 13:37:38: `/resume` fired twice
(`auto_slash_command command=/resume`) in the same second. Traced (by code
read) to the **only** call site of that specific command:
`user_actions.py:_on_resume_clicked` → `status_header.py:484`
(`self._btn_resume.clicked.connect(self._on_resume_clicked)`). The mobile
resume/session-picker feature (`remote/api.py:resume_lead`) is a *different*
code path (`orch.close()` + `orch.spawn(..., resume_uuid=...)`, not
`inject_slash_command_when_ready`), so it is not the source of this specific
duplicate.

Structural observation: unlike the remote-control bridge (deduped by
session-uuid via `_lead_remote_bridge_fired`/`_pending`),
`inject_slash_command_when_ready` has **no cross-call dedup at all** — each
call's `sent[0]` flag is local to that call's closure. Two independent
invocations of the same command (e.g. two `clicked` signal deliveries for
one button press, or a signal connected twice) are not deduped against each
other in any way. Whether `_btn_resume`'s `clicked` signal is genuinely
double-connected (e.g. a status-bar rebuild path re-running
`clicked.connect` without a matching `disconnect`) was **not** confirmed —
only the single `connect()` call site was found via grep, so a rebuild path
calling that same setup code twice is the leading unconfirmed hypothesis,
not a proven cause. Left for a follow-up investigation per the task's scope
note ("แค่ report ไม่ต้องแก้ในงานนี้").

## Tests

- `tests/test_remote_bridge_repro.py` (new, 12 tests) — real-timing repro:
  every drop path + reason, delivery-still-works path, reaper retry wiring,
  `/resume` re-fire, non-Lead no-op.
- `tests/test_remote_bridge_autofire.py` — updated call-signature
  assertions for the new `max_wait_ms`/`on_delivered`/`on_dropped` kwargs;
  behavior assertions unchanged.
- Targeted run green: `test_remote_bridge_repro.py`,
  `test_remote_bridge_autofire.py`, `test_consume_session_report.py`,
  `test_lead_draft_guard.py`, `test_idle_watchdog.py`,
  `test_cli_server_session_report.py`.
- `ruff check` / `ruff format --check` clean; `lint-imports` 18/18 contracts
  kept.
- Per the "targeted tests only" project rule, the full suite was **not**
  run for this task (no behavior-neutral-refactor claim here) — qa's batch
  gate should run it before merge.
