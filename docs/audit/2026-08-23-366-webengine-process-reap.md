# #366 — QtWebEngineProcess reap: soak-harness bug, not an app teardown bug

**Pane:** devops · **Date:** 2026-08-23 · **Machine:** this dev machine, win32,
real windowed desktop session (not headless CI).

## Summary

`docs/audit/2026-08-23-365-workspace-ram-acceptance.md` §2 found that after
`EditorHost.close_all()`/`PreviewHost.close_project()`, `QtWebEngineProcess`
child processes never exited within 30s, measured under
`QT_QPA_PLATFORM=offscreen` + `--disable-gpu`, with an open caveat that this
might be a headless/GPU artifact rather than a real bug. **It is neither** —
it reproduces identically on a real windowed display, and the root cause is
in `tools/soak_workspace_webengine.py`'s own event-loop pumping style, not in
`editor_widget.py`/`preview_widget.py`'s teardown code, which needed no
changes.

## Step 1 — verify on real display (per issue's own instructions)

Added `--real-display` to the soak tool (skips `QT_QPA_PLATFORM=offscreen`
and `--disable-gpu`/`--disable-gpu-compositing`). Result, unchanged code,
5 cycles / 3 projects, 35s poll window:

```
webengine_process_count_before: 0
webengine_process_count_after: 4
webengine_reap_seconds: null   (never reached baseline within 35s)
```

Same shape as the offscreen measurement — **not** a headless artifact.
Removing `--renderer-process-limit=4` from the Chromium flags to rule out
"coincidentally capped at the limit value" produced 5 stuck processes for 5
cycles (1:1 with cycle count, unbounded), confirming a real per-cycle
accumulation, not a coincidence with that flag's value.

## Step 2 — isolate the actual cause

A minimal probe (`EditorHost` with a real, `.show()`n container, single
open/close cycle, `sip.isdeleted()` checked on the page/view/`_EditorWebView`
objects every second for 30s) showed **all three stayed `isdeleted() ==
False` for the entire 30s** — `deleteLater()` was scheduled but the
`QEvent::DeferredDelete` was never actually delivered, despite 30 explicit
`QApplication.processEvents()` calls (the soak tool's `_pump()` helper: a
`while` loop alternating `processEvents()` and `time.sleep()`).

A second, app-code-free probe (bare `QWebEngineView`, `page.deleteLater()` +
`view.deleteLater()`, then a **real nested `QEventLoop().exec()`** instead of
`processEvents()`-in-a-sleep-loop) reaped in **~1s**. Re-running the
`EditorHost`/`PreviewHost` probes the same way (a real `QTimer`-driven
`app.exec()`) reaped in **~1s** and **~1s** respectively, with zero app-code
changes.

**Conclusion:** Chromium's WebEngine deferred-delete / IPC shutdown
handshake needs a genuinely blocking, waiting event loop (`QEventLoop.exec()`
/ `app.exec()`) to complete — bare `processEvents()` calls, even called
repeatedly across 30+ seconds, never deliver it on this Qt/Windows build.
Since the production app (`main_window.py`) always runs under `app.exec()`,
`EditorHost`/`PreviewHost`'s existing teardown sequencing (`deleteLater()` on
page → view → profile, in that order) was already correct and never leaked
in the real running app — only this soak harness's own pumping style did.

## Step 3 — fix

`tools/soak_workspace_webengine.py`: replaced the `_pump()` helper's
`processEvents()`+`time.sleep()` body with a real nested `QEventLoop`
(`QTimer.singleShot(ms, loop.quit); loop.exec()`), and switched the
final settle delay + the new reap-poll loop to the same mechanism. No
changes to `editor_widget.py` or `preview_widget.py`.

Re-run with the fixed harness:

| scenario | cycles/projects | `webengine_process_count_after` | `webengine_reap_seconds` |
|---|---:|---:|---:|
| real display | 5 / 3 | 0 | 1.1 |
| real display | 15 / 3 | 0 | 0.5 |
| offscreen (default) | 5 / 3 | 0 | 0.5 |

Editor RAM numbers otherwise unchanged (RSS delta, `stuck_open_cycles`,
`stuck_closed_cycles` all still clean — this was purely a process-count
sampling bug, not a memory or lifecycle regression).

## Step 4 — gate + docs

- Added `test_editor_webengine_process_reaped_after_close` to
  `tests/test_workspace_webengine_soak.py` (opt-in
  `AGENT_TAKKUB_QT_WEBENGINE_SMOKE=1`, same as the rest of the file) —
  asserts `webengine_process_count_after <= webengine_process_count_before`.
  Tolerance is exact (not a fudge factor): the fixed harness reaps in
  0.5–1.5s against a 30s default timeout, so any leftover process is a real
  regression signal, not pump-timing noise.
- `tools/soak_workspace_webengine.py`'s own `ok` gate now includes the same
  check, so a plain manual run (`--json-out`) reports `"ok": false` on a real
  regression too, not just the pytest wrapper.
- `docs/audit/2026-08-23-365-workspace-ram-acceptance.md` §2 "≈0 after
  close" row: was ❌ (never reached 0 in 30s) → now **✅ 0, reaps in ~0.5–1.5s**
  with the fixed harness, confirmed on both real display and offscreen.
- `CHANGELOG.md` `[vNEXT]` entry added under `### Added`.

## What this means for future soak/measurement scripts in this codebase

Any script that constructs a real `QWebEngineView`/`QWebEnginePage` and needs
to observe its teardown (process exit, `deleteLater` completion, RSS after
close) **must** pump via a real nested `QEventLoop`/`app.exec()`, not bare
`QApplication.processEvents()` calls in a Python sleep loop — the latter will
under-report teardown as "stuck" indefinitely on this Qt/Windows build,
independent of `QT_QPA_PLATFORM`. `tools/spike_pane_discard_ram.py` and any
other WebEngine-touching soak tool should be checked against this same
pattern if they ever need to assert on post-close process state.
