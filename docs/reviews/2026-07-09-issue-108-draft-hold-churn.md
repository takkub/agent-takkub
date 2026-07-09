# Issue #108 — draft-hold spill↔flush churn loop

## Repro (proven from code + events.log timing, not guessed)

Sequence that produced the observed loop (2 done notices, every ~5s, for ~2min):

1. Durable `_pending_done_notices[project]` holds 2 items; Lead is alive and
   `is_at_ready_prompt()` reads True (idle), but the Lead pane's draft state
   has been `NONEMPTY`/`UNKNOWN_NONEMPTY` for well over `DRAFT_HOLD_TIMEOUT_S`
   (180s) and never clears.
2. `_reap_pending_done_notices` only checked `is_at_ready_prompt()`, so it
   called `_flush_pending_done_notices`, which looped `for item in items:
   self._notify_lead(project_ns, item["body"])` — **one item at a time**.
3. Each `_notify_lead` call appends 1 item to the live `_lead_notify_queue`
   and arms `_pump_lead_notify` **synchronously**. Since `draft_hold_expired`
   was already stuck `True` (the draft's `pending_since` never resets because
   the draft never clears), the pump spilled that single item straight back
   to durable **in the same call stack**, before the next loop iteration ran.
4. Result: 2 items → 2 separate `lead_notify_draft_spill` log lines (each
   `count=1`) — the observed "x2" — plus 1 `done_notices_flushed` log, all
   within the same 5s reaper tick, with the items landing right back where
   they started. Next tick, repeat.
5. `_pending_done_since` (the #70 staleness clock) got popped/reset on every
   tick because the code took the "ready" branch (`is_at_ready_prompt()` was
   true) even though nothing actually got delivered — so the #70 force-flush
   escalation couldn't accumulate normally on this path; it only ultimately
   fired because of a separate stall window quirk, not by design.

## Fixes applied (`src/agent_takkub/lead_inbox.py`)

1. **`_flush_pending_done_notices`** now checks `_lead_can_accept_injection`
   before moving anything out of the durable queue. If a draft is pending it
   returns immediately — items stay parked in durable, no log spam, no
   spill/refill cycle.
2. **`_reap_pending_done_notices`** now gates the "flush now" branch on
   `is_at_ready_prompt() and _lead_can_accept_injection(...)` together, and
   folds "ready but draft-blocked" into the *same* staleness-accumulating
   branch as "not-ready" (previously only "not-ready" accumulated
   `_pending_done_since`; "ready but draft-blocked" incorrectly reset it every
   tick). This preserves the #70 force-flush safety net for a draft that's
   genuinely stuck: after `_DONE_NOTICE_STALE_S` (60s) of being unable to
   flush — for either reason — `_force_deliver_done_notices` bypasses every
   gate (including the draft-hold gate) and delivers.
3. **Duplicate x2 spill** root cause was the per-item loop in
   `_flush_pending_done_notices` each independently arming/spilling the pump.
   Fixed as a side effect of (1): flush no longer hands items to the live
   pump at all while blocked, so the per-item spill fragmentation can't occur
   via the reaper path.

## Item 4 — false-positive draft state investigation

Read `lead_draft_state.py` end to end. Focus-in/out (`\x1b[I` / `\x1b[O`) and
bracketed-paste start/end (`\x1b[200~` / `\x1b[201~`) are already handled
correctly:
- Focus in/out match the generic CSI pattern and fall through to the
  "cursor-movement/no-op" case — they were never the "any other CSI" branch's
  special case, so the state is unchanged either way. Added regression tests
  (`TestFocusInOut` in `tests/test_lead_draft_state.py`) pinning this — no
  bug found here, so no fix was needed for that path.
- Paste start correctly enters `UNKNOWN_NONEMPTY` (length genuinely unknowable
  from the byte stream) and paste end is a no-op CSI, matching existing
  `TestBracketedPaste` coverage.

Did **not** find a provable false-positive in the state machine itself. The
observed multi-minute "pending" read is consistent with either (a) a real
unsubmitted draft the operator left sitting, or (b) `UNKNOWN_NONEMPTY` from an
Up/Down history recall or paste that was never followed by Enter/Esc/Ctrl+C/
Ctrl+U — both are working as designed (conservative hold, by design). No
speculative fix applied here since none could be proven; the reaper-side fix
(items 1-3) is what actually stops the churn regardless of why the draft
reads pending.

## Tests

- `tests/test_done_notice_draft_churn.py` (new): reproduces the exact #108
  shape (2 durable notices + stuck draft + repeated 5s reaper ticks) and
  asserts zero churn events; separately proves the #70 force-flush safety net
  still fires after `_DONE_NOTICE_STALE_S`.
- `tests/test_lead_draft_state.py`: added `TestFocusInOut` regression class.
- Full targeted run: `test_lead_draft_state.py`, `test_done_notice_draft_churn.py`,
  `test_reap_multiproject.py`, `test_orchestrator_notify_lead.py`,
  `test_cross_tab_done.py`, `test_lead_draft_guard.py` — **97 passed**.
- `ruff check` / `ruff format --check` clean; import-linter **18/18 kept**.
- Full suite intentionally not run (targeted-tests-only rule for mid-task work).
