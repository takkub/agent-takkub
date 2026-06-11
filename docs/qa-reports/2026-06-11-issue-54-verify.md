# QA Verification Report — Issue #54
**Date:** 2026-06-11  
**Scope:** Wire `is_blocked_on_tty_prompt()` into idle + stuck watchdog (orchestrator.py)  
**Branch state:** uncommitted changes in `src/agent_takkub/orchestrator.py` (+83 lines)

---

## 1. Test suite run

```
python -m pytest tests/test_idle_watchdog.py tests/test_stuck_recover.py tests/test_lifecycle_recovery.py -v
```
**Result: 81/81 PASSED**

Full suite:
```
python -m pytest -q --tb=no
```
**Result: 2 FAILED** — both in `tests/test_pane_transcript.py::TestTerminateClosesTranscript`  
Root cause: QtWebEngine import-ordering (`QWebEngineView` must be imported before `QCoreApplication` is created) — pre-existing, unrelated to #54. Confirmed by error message:
```
ImportError: QtWebEngineWidgets must be imported or Qt.AA_ShareOpenGLContexts must be set before a QCoreApplication instance is created
```

---

## 2. Behavior verification

### (a) TTY-blocked pane → does NOT fire `IDLE_REMINDER_TEXT`
**PASS**  
Test: `TestTtyBlockIdleWatchdog::test_blocked_pane_does_not_fire_forgot_done_reminder`  
Code path: `_check_idle_teammates()` calls `is_blocked_on_tty_prompt()` → if truthy, sets `first_idle_ts = None` and calls `_maybe_surface_tty_block()`, then `continue` (skips reminder).

### (b) Surface ⚠️ notice to Lead after `TTY_BLOCK_SURFACE_AFTER_S` (2 min), with `TTY_BLOCK_SURFACE_COOLDOWN_S` (3 min) anti-spam
**PASS**  
Tests:
- `test_blocked_pane_surfaces_notice_to_lead_after_threshold` — notice fires only after threshold, written to Lead's session  
- `test_surface_notice_respects_cooldown` — no re-surface within cooldown; re-fires after cooldown elapses  
Helpers: `_maybe_surface_tty_block()` + `_surface_tty_block_notice()` wired in `_check_idle_teammates()`

### (c) Stuck-recover defers close→respawn when pane is TTY-blocked
**PASS**  
Tests (`TestTtyBlockStuckDefer`):
- `test_tty_blocked_pane_defers_stuck_recover` — `close_calls == []` when `is_blocked_on_tty_prompt` returns truthy  
- `test_tty_blocked_pane_surfaces_notice_immediately_in_stuck_context` — notice fires immediately (no `TTY_BLOCK_SURFACE_AFTER_S` wait, already past `STUCK_THRESHOLD_S`)  
- `test_tty_surface_notice_respects_cooldown` — cooldown gates re-surface in stuck path too  
Code path: `_check_stuck_panes()` — after threshold check, calls `is_blocked_on_tty_prompt()` → if truthy, sets `tty_blocked_since`, calls `_surface_tty_block_notice()`, `continue` (skips `_auto_recover_stuck`)

### (d) REGRESSION: non-blocked pane still fires forgot-done reminder + non-TTY wedge still auto-recovers
**PASS**  
Tests:
- `TestTtyBlockIdleWatchdog::test_normal_idle_pane_behavior_unchanged` — `tty_prompt=None` pane still gets `IDLE_REMINDER_TEXT` after threshold  
- `TestTtyBlockStuckDefer::test_non_blocked_pane_still_recovers` — `is_blocked_on_tty_prompt()=None` pane still triggers `close_calls`

---

## 3. Auto-Ctrl-C / auto-close by default
**PASS (conservative)**  
Grep for `Ctrl.C|SIGINT|auto.ctrlc` in `orchestrator.py` → **no matches**.  
`_surface_tty_block_notice()` only injects a text notice to Lead's input + emits `leadInjected` signal. No automatic Ctrl-C, no `session.write(b"\x03")`, no close/respawn of the blocked pane.

---

## 4. Import sanity
```
python -c "import agent_takkub.orchestrator, agent_takkub.pty_session; print('import OK')"
```
**Result: import OK**

---

## 5. Constants & fields
| Symbol | Value | Location |
|---|---|---|
| `TTY_BLOCK_SURFACE_AFTER_S` | 120 s (2 min) | `orchestrator.py:375` |
| `TTY_BLOCK_SURFACE_COOLDOWN_S` | 180 s (3 min) | `orchestrator.py:376` |
| `PaneState.tty_blocked_since` | `float \| None = None` | `orchestrator.py:842` |
| `PaneState.last_tty_block_surface_ts` | `float = 0.0` | `orchestrator.py:843` |
| `is_blocked_on_tty_prompt()` | real method in `pty_session.py:584` | cursor-anchored scan, returns matched line or `None` |

---

## 6. Gap analysis
**No gaps found.** All 4 behavior requirements covered by tests. No missing edge cases:
- Block detected → never clears: handled (clear path when `is_blocked_on_tty_prompt()` returns `None`, sets `tty_blocked_since = None`)
- Both watchdog paths (idle + stuck) independently handle TTY block
- `lifecycle_recovery.py` stubs added for `_surface_tty_block_notice` and `_maybe_surface_tty_block` in `_FakeOrchForContentDelta` — ensure content-delta tests don't get TTY-block interference

---

## Summary
**PASS** — Issue #54 implementation correct.  
81/81 targeted tests pass · 2 pre-existing QtWebEngine failures (unrelated) · no auto-Ctrl-C · import clean · all 4 behavior requirements verified by tests + code inspection.
