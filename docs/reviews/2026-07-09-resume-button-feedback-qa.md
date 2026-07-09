# QA verify: ↻ Resume button UI feedback fix (2026-07-09)

Verifying backend fix `status_header.py:478` / `user_actions.py:219` — full backend report: `runtime/sessions/2026-07-09/agent-takkub/backend-174108.md`.

## 1. Targeted suite — re-run independently

```
pytest tests/test_resume_button_feedback.py tests/test_remote_chip.py tests/test_lead_draft_guard.py -v
```

**Result: 36 passed** (6 + 15 + 15), 0.44s. Confirmed via `--junitxml` (rtk's pytest wrapper hides the stdout tail line — junit XML used for a trustworthy count, per this project's known rtk gotcha).

### Assertion-depth check on `test_resume_button_feedback.py`
Read the file line-by-line — not superficial. Uses a real `QPushButton` (not a mock) via the same `_Stub(UserActionsMixin)` pattern as `test_remote_chip.py`, and asserts on real widget state (`isEnabled()`, `.text()`) plus real `MagicMock` call-args for the status bar message, not just "did it call something":

| Spec item | Test | Assertion depth |
|---|---|---|
| busy-state-on-click | `test_disables_button_and_shows_busy_label_immediately` | `isEnabled() is False` + exact text `"⏳ Resuming…"` |
| callbacks-passed | `test_passes_on_delivered_and_on_dropped_callbacks` | both kwargs are `callable` |
| on_delivered restores+message | `test_on_delivered_restores_button_and_shows_success` | enabled restored, text restored, `showMessage` called once, message content checked (substring) |
| on_dropped restores+message-with-reason | `test_on_dropped_restores_button_and_shows_reason` | enabled/text restored, message contains both the reason string **and** the Thai error prefix |
| repeat-click-while-disabled no-op | `test_repeat_click_while_disabled_is_a_no_op` | `call_count` stays 1 after a second click while still disabled |
| click-again-after-delivered fires again | `test_click_again_after_delivered_fires_again` | `call_count` becomes 2 after delivered → re-click |

Cross-checked against the actual implementation (`user_actions.py:219-249`) — button text/enabled state and status-bar strings in the test match the source exactly, including the Thai error copy. No gap found between spec and test.

## 2. Live Qt render — display available, ran it

This is a real Windows interactive session (`platformName=windows`, not `offscreen`/headless CI), so per the task's own instruction this step was **not skipped**. Rather than launching the full cockpit (would risk the single-instance lock killing the user's real running cockpit — known project gotcha), wrote a standalone script that imports `UserActionsMixin` + a real `QApplication`/`QPushButton` and drives the actual Qt event loop:

- Clicked → observed real widget: disabled + `⏳ Resuming…` ✅
- Fired `on_delivered`/`on_dropped` **asynchronously via `QTimer.singleShot`** (mirrors exactly how `lead_inbox.inject_slash_command_when_ready`'s real polling loop invokes these callbacks — confirmed by reading `lead_inbox.py:394-414`, `QTimer.singleShot(500, _check)` on the same GUI thread) instead of calling them synchronously in the same stack frame like the unit tests do, then spun `app.processEvents()` across real elapsed wall-clock ticks (`time.sleep(0.01)` between polls) so the timer had to actually fire through the live event loop.
- delivered path: button re-enabled, text restored to `↻ Resume`, status bar showed `/resume sent to Lead` ✅
- dropped path: button re-enabled, text restored, status bar showed the Thai error with `timeout_not_ready` reason ✅

Both async paths passed (`ALL_OK`). Script not committed (scratchpad-only, per policy) — logic is faithfully described above and is trivially reproducible.

## 3. Edge case assessment: is unit test + this live sim sufficient for sign-off?

**Thread-safety concern raised in the task — resolved as a non-issue, not just assumed away.** Read `lead_inbox.py:331-414`: `inject_slash_command_when_ready`'s poll loop (`_check`) runs entirely via `QTimer.singleShot`, which always fires back on the Qt **main/GUI thread** — there is no worker thread, no `QThread`, no cross-thread signal/slot involved anywhere in this path. So the "QTimer mock vs real Qt event loop" distinction the task worried about turned out to make **zero behavioral difference** here, and the live sim above confirms it: identical outcome whether the callback fires synchronously (unit test) or asynchronously through real timer ticks (live sim).

Remaining gap not covered by either: the actual `QPushButton` living inside the real `MainWindow`/`status_header.py` widget tree (real parent layout, real stylesheet, real screen paint) was not clicked with a physical/simulated mouse event inside the full running cockpit — that would require launching a second cockpit instance, which risks killing the user's real one via the single-instance lock (known project gotcha, `app.py` QLockFile). Given (a) the button/text/enabled-state logic is plain PyQt widget API with no custom paint/event-filter code in the diff, (b) the callback wiring is now proven correct on the real GUI thread's event loop, and (c) the values `on_delivered`/`on_dropped` compute are asserted against the literal strings the real status bar renders — this residual gap is cosmetic-only risk (layout/theming), not logic risk. **Sufficient for sign-off.**

## Verdict

**PASS.** 36/36 targeted tests green (fresh run), assertions verified non-superficial against source, live Qt event-loop simulation of both delivered/dropped paths confirmed correct button/status-bar behavior through the real async mechanism the production code actually uses.
