# #365 Workspace 1.2.0 — RAM acceptance + 16_ACCEPTANCE_CRITERIA sign-off

**Pane:** frontend · **Date:** 2026-08-23 · **Scope:** frontend leftover task after
phase 3–5 security review + phase 5/6 (`preview_widget.py`, Design Director UI) merge.
Covers: (1) the review's UI-scoped SHOULD items, (2) master plan §4's RAM acceptance
scenario, (3) `16_ACCEPTANCE_CRITERIA.md`'s 19 items against real `main` state.

---

## 1. Reviewer SHOULD items closed (2026-08-23 phase 3-5 review)

Source: `docs/reviews/2026-08-23-workspace-1.2.0-phase3-5-review.md`. The MUST-FIX
(finding #1, IPC pane-token gate for `preview`/`design`) is backend/cli_server-owned,
not in this task's scope — only the three UI-scoped items below were assigned here.

### (a) Finding #5 [NIT] — CHANGES-row path now goes through `resolve_and_contain`

`project_explorer.py`'s `_on_changes_changed` built `_PATH_ROLE` from
`(repo_root / change.path).resolve()` directly — the one path in the module that
skipped the containment gate every other path goes through (per the module's own
docstring). Fixed: now calls `resolve_and_contain(repo_root / change.path,
list(self.roots.values()))` and skips (not crashes) a row that fails containment.
Not independently exploitable before the fix (git status output can't contain `..`
for a well-formed working tree, and both downstream consumers already re-validate),
but now consistent with the module's stated invariant.

Test added: `test_row_path_escaping_roots_is_skipped_not_crashed` in
`tests/test_project_explorer.py` (a `../outside.py` change row is silently dropped,
an in-root row survives).

### (b) Finding #4 [SHOULD] — `navigation_allowed` contract pinned + verified against the real widget

`preview_widget.py` (frontend-built, phase 5) already exists and already calls
`navigation_allowed` correctly for the cases the review flagged as open questions:

- **Redirects**: `_PreviewPage.acceptNavigationRequest` never filters on `nav_type`,
  so every top-level `NavigationRequest` Chromium raises — including each hop of a
  server-side HTTP redirect, which QtWebEngine re-queries `acceptNavigationRequest`
  for individually — reaches `nav_check` automatically. No separate redirect hook
  needed or added.
- **New-window/popup** (`target="_blank"`, `window.open()`): `_PreviewPage` has no
  `createWindow` override, so `QWebEnginePage`'s own default (returns `None`,
  silently refuses the popup) applies — confirmed as the actual shipped behavior via
  the identical, already-documented case in `terminal_widget.py`'s `_on_open_url`
  ("WebLinksAddon's default handler uses `window.open()`, which QtWebEngine silently
  blocks (no `createWindow` override)"). This is correct and matches the "exactly ONE
  Preview widget, never a second window" rule — deliberately left un-overridden
  rather than adding a redundant explicit block.
- **Iframe (sub-frame) navigation**: deliberately bypassed (`is_main_frame=False`
  returns `True` without consulting `nav_check`) — the threat model's concern is the
  top-level page, not already-sandboxed embedded content. This was already the
  design; now stated as an explicit contract, not just a class-docstring aside.

Contract now pinned in `preview_controller.navigation_allowed`'s docstring (the
policy-decision function itself) and cross-referenced from `_PreviewPage`'s class
docstring in `preview_widget.py`, so a future edit to either can't silently drift
from the other.

Tests added in `tests/test_preview_widget.py`, `TestPreviewPageNavigationContract`
(exercises `_PreviewPage.acceptNavigationRequest` as an unbound function against a
duck-typed stand-in — no real `QWebEnginePage` construction needed, since the method
body only touches plain `self._nav_check`/`self._on_blocked` attributes):

- main-frame allow / refuse-and-report
- `nav_check` consulted independently of `nav_type` (redirect coverage) — swept
  across five stand-in nav-type values
- sub-frame bypass (nav_check never called for `is_main_frame=False`)
- `nav_check` raising fails closed
- `createWindow` absence pinned (`"createWindow" not in vars(pw._PreviewPage)`) so a
  future override is forced to notice it must route through `nav_check` too

### (c) Ask Agent 4000-char bound — test coverage gap closed

The review's "What's solid" section confirmed both the client-side (`static/editor/
index.html:713`) and server-side (`main_window.py:487`, actually line 558 —
`(selected_text or "")[:4000]`) bounds exist and slice correctly, but that was a
code-read confirmation, not a test — no test file exercised
`MainWindow._on_editor_ask_agent`'s server-side bound at all (`grep` across `tests/`
for `_on_editor_ask_agent`/`ask_agent` found none). Added
`tests/test_main_window_ask_agent.py`, same `Mock(self)` + unbound-method-call
pattern `test_main_window_status_bar.py`'s `TestOnTabSwitchedNoTabsLeft` already uses
(no Qt app needed — the method only touches `self.orch` + a module-level
`project_roots` lookup):

- a 9000-char selection is truncated to exactly 4000 chars before it reaches the
  fenced block in the message sent to Lead
- a selection under the bound passes through unchanged
- an empty selection omits the fenced code block entirely
- the message is routed to `LEAD.name` with `from_role="editor"`
- a `send` failure is logged, not raised

All four targeted files pass together:
`tests/test_main_window_ask_agent.py tests/test_preview_widget.py
tests/test_preview_controller.py tests/test_project_explorer.py` — 
`takkub qa-gate --targeted` (pytest + ruff + import-linter) green.

---

## 2. RAM acceptance (master plan §4)

> "`16_ACCEPTANCE_CRITERIA.md` ทั้ง 19 ข้อ + RAM: วัดก่อน/หลังเปิด editor+preview บน 3
> โปรเจกต์ — เพิ่มไม่เกิน +300 MB รวม และ 0 เมื่อปิด"

### Method

`tools/soak_workspace_webengine.py` (devops-owned, opt-in) cycles editor open/close
per-project with the preview side only exercising the **headless**
`PreviewController` state machine — it predates `preview_widget.py` (this was built
in the same phase this leftover task is closing out), so it never constructs a real
Preview `QWebEngineView`. That's the right tool for *lifecycle-crash* regression
(item 18 below) but not for *this specific* "editor+preview simultaneously open on 3
projects" RAM number, so a second one-off script
(not committed — ad-hoc measurement, mirrors the soak tool's hard requirements:
import QtWebEngineWidgets before any `QCoreApplication`, `QApplication(sys.argv)`)
drove the actual scenario: a real `EditorHost` with 3 projects' files open
simultaneously (multi-tab, one shared view) + a real `PreviewHost` cycled across the
same 3 projects' local HTML files (file mode, one shared view, landing on project 3),
both real `QWebEngineView`s. `QT_QPA_PLATFORM=offscreen`, same Chromium flags the
existing soak tool uses. Run on this machine (Windows, win32).

### Results — opening (the acceptance number)

| | before | after open (3 projects, editor+preview) | delta |
|---|---:|---:|---:|
| main process RSS | 45.4 MB | 97.0 MB | **+51.6 MB** |
| WebEngine child processes RSS | 0.0 MB | 103.0 MB | **+103.0 MB** |
| WebEngine process count | 0 | 2 (editor + preview, each its own off-the-record profile) | |
| **total** | | | **≈ +154.6 MB** |

**PASS against the ≤ +300 MB budget** — well under, with headroom (editor and
preview each get their own `QWebEngineProfile`/renderer process by design, which is
the dominant cost here, not per-project multiplication — RAM rule already guarantees
no second view is ever created per project).

### Results — after close (the "≈0" number): does NOT return to baseline in this harness

`editor_host.close_all()` + `preview_host.close_project(...)` were called, then RSS
was sampled on a decay curve (5/10/15/20/30s after close, `gc.collect()` before each
sample) rather than one fixed point, since the first single-sample run (10s) hadn't
settled either:

| elapsed after close | main RSS | WebEngine RSS | WebEngine process count |
|---:|---:|---:|---:|
| 5s | 97.1 MB | 104.5 MB | 2 |
| 10s | 98.0 MB | 104.5 MB | 2 |
| 15s | 96.0 MB | 106.9 MB | 2 |
| 20s | 96.0 MB | 106.9 MB | 2 |
| 30s | 96.0 MB | 106.9 MB | 2 |

The WebEngine process count **never drops from 2 to 0** within 30s, and RSS is flat
(not decaying) after ~15s — this is not "still tearing down slowly", it looks
genuinely stuck. `close_all()`/`close_project()` do call `deleteLater()` on
page/view/profile (confirmed by reading `editor_widget.py`/`preview_widget.py`), and
`has_view()` does flip to `False` immediately (that's a Python-level flag set before
`deleteLater()`, not a signal that the OS process actually exited) — so the
Python-level teardown path runs, but the native `QtWebEngineProcess` subprocess
itself is not observed to exit.

**Corroborating evidence this is a real gap, not a one-off artifact of this script:**
re-ran the existing `tools/soak_workspace_webengine.py --cycles 5 --projects 3`
(devops' own tool, editor-only real WebView cycling — open then close, 5 times) and
its own output shows the same pattern, worse because it accumulates per cycle:

```
"webengine_process_count_before": 0,
"webengine_process_count_after": 4,
"webengine_rss_mb_before": 0.0,
"webengine_rss_mb_after": 375.6,
```

Every open/close cycle appears to leave behind a `QtWebEngineProcess` that is never
reaped — 5 cycles, 4 stray processes, 375.6 MB. **This passes the soak tool's own
`ok` gate and its pytest wrapper
(`tests/test_workspace_webengine_soak.py::test_editor_open_close_leaves_no_stuck_cycles`)**
because both only check the Python-level `has_view()`/`open_count()` flags
(`stuck_open_cycles`/`stuck_closed_cycles`), never `webengine_process_count_after` —
so this accumulation is currently invisible to the existing regression gate.

**Caveat on this finding**: measured under `QT_QPA_PLATFORM=offscreen` with
`--disable-gpu`; Chromium logs a GPU context failure on every run
(`Failed to create GLES3 context... Failed to create shared context for
virtualization`) — a known category of quirk under headless/no-display Chromium.
Whether this specific accumulation is (a) a genuine bug in this codebase's teardown
sequencing, or (b) an artifact of renderer processes never fully handshaking their
GPU process under `--disable-gpu`+offscreen and therefore never reaching a state
where they'll exit cleanly, is **not resolved by this measurement** — it needs
verification under a real windowed session (which this task cannot produce: no
display in this environment, and browser-driver tooling is out of scope for this
role) before concluding which.

**Given the "report real numbers, don't force a pass" instruction for this task**:
the +300 MB open-side budget is met with real headroom (✅); the "≈0 after close"
side is **not met in this harness within 30s** (❌ as measured, with the GPU/offscreen
caveat above) — not silently marked passing. Recommend as follow-up (not done here,
out of this task's scope): (1) add `webengine_process_count_after` assertions to
`tests/test_workspace_webengine_soak.py` so this stops being invisible to the
existing gate, (2) re-run this same measurement on a real display before the 1.2.0
release RAM sign-off, since a raw process-not-reaped bug (if that's what this is)
would matter far more on a long-lived desktop session than the +300MB open-side
number does.

Raw JSON from both runs saved during this session (not committed — reproducible via
the commands below):

```
# one-off 3-project simultaneous open/close scenario
.venv/Scripts/python.exe <ad-hoc script, not committed>

# devops' own soak tool, 5 cycles / 3 projects
.venv/Scripts/python.exe tools/soak_workspace_webengine.py --cycles 5 --projects 3
```

---

## 3. `16_ACCEPTANCE_CRITERIA.md` — 19 items against real `main` state

Legend: ✅ verified (code + automated test) · ⏳ code-verified, needs a real
browser/GUI session to fully confirm (none available in this environment/role) ·
❌ found broken by this review.

| # | Criterion | Status | Evidence |
|---|---|:---:|---|
| 1 | Explorer collapsible/resizable and root-safe | ✅ | `project_tab.py`: `QSplitter` (resizable), `explorer.setVisible()` toggle (collapsible), `ProjectExplorer(project_name)` construction wrapped in `try/except` — a malformed `projects.json`/no-roots case degrades to "no explorer" instead of breaking the tab. |
| 2 | Text file opens/editable in Takkub with local Monaco | ⏳ | `editor_widget.py`/`editor_service.py` + `static/editor/index.html` implement it; unit/stub-tested (`test_editor_widget.py`, `test_editor_service.py`) and the opt-in real-`QWebEngineView` smoke test exists but wasn't run here (needs `AGENT_TAKKUB_QT_WEBENGINE_SMOKE=1` + ideally a real display) — visual Monaco rendering itself needs eyes-on. |
| 3 | Atomic save | ✅ | `editor_service.save_atomic`/`_write_atomic_text`: same-dir temp + `os.replace`, verified against `test_editor_service.py`'s CRLF/BOM/Thai-path/symlink-escape cases (confirmed solid by the phase 3-5 review). |
| 4 | Concurrent disk change never silently overwrites | ✅ | mtime_ns+size+sha256 conflict rule, server-tracked baseline never updated by a disk-watch echo (`EditorHost._file_states`) — confirmed by the phase 3-5 review + `test_editor_service.py`/`test_editor_widget.py`. |
| 5 | Git changes and diff visible | ⏳ | `git_changes_service.py` (CHANGES panel, this task's fix #a above) + `EditorHost.requestDiff` bridge slot exist and are unit-tested; the actual diff *rendering* in Monaco's diff view needs eyes-on in a real session. |
| 6 | Local app URL opens in per-project Preview | ⏳ | `PreviewController.open_url` (loopback-gated) → `PreviewHost.show_state` → real `QWebEngineView.load_url` — code + stub-view tested end-to-end (`test_preview_controller.py`, `test_preview_widget.py`'s `TestControllerSignalsDriveTheWidget`); never verified against an actual running dev server + real Chromium render in this pass. |
| 7 | HTML design artifact opens in Preview | ⏳ | `design_actions.publish_design_artifact` → `preview_controller.open_file` (containment+extension gated) → `PreviewHost.show_state` file mode — same code-path coverage/caveat as #6. |
| 8 | Desktop/tablet/mobile presets | ⏳ | `preview_widget.DEVICE_FRAME_SIZES` + `PreviewHost.set_device`/`apply_device` unit-tested (`TestDevicePresets`); real fixed-viewport layout rendering needs eyes-on. |
| 9 | Designer publish auto-focuses Preview | ✅ | `publish_design_artifact` calls `preview_controller.open_url`/`open_file` → `Orchestrator.previewOpened` → `MainWindow._on_preview_opened`: `self._preview_dock.show(); self._preview_dock.raise_()` — traced end-to-end in `main_window.py:299-312,499-501`. |
| 10 | Approve -> structured Lead notice | ✅ | `design_actions.approve` → `_notify_lead` (`orchestrator.py:6155-6160` per the phase 3-5 review) — confirmed by that review as "a real, convincing-looking message injected into ... Lead pane". |
| 11 | Revise -> structured Designer feedback | ✅ | `design_actions.request_revision`, same notify pattern; `PreviewHost._on_revise_clicked` collects feedback via `QInputDialog` and emits `reviseRequested`, tested end-to-end in `TestApproveReviseFlowThroughRealDesignActions`. |
| 12 | Existing AgentPane/project switch behavior works | ✅ | No changes to `agent_pane.py`/tab-switch core logic in this epic; not independently re-verified in this pass beyond "not touched". |
| 13 | Graft remains structural code intelligence | ✅ | Not touched by #365 (no code overlap found); unrelated subsystem. |
| 14 | Brain/Conversation remain canonical memory/session | ✅ | Not touched by #365; unrelated subsystem. |
| 15 | Obsidian remains curated human knowledge | ✅ | Not touched by #365; unrelated subsystem. |
| 16 | OpenViking optional | ✅ | Not touched by #365; unrelated subsystem/flag. |
| 17 | No new heavy IO on Qt main thread | ✅ | Confirmed by the phase 3-5 review's "Concurrency/IO" item: every git subprocess call sets `timeout=`; all FS/git work runs on `QThreadPool` workers; `FileWatchService`'s handler only queues a debounced flush, actual stat+hash happens in `_SnapshotWorker.run`. |
| 18 | Windows WebEngine soak has no lifecycle/reparent crash regression | ✅ | Ran on this machine (win32): both the one-off 3-project script and `tools/soak_workspace_webengine.py --cycles 5 --projects 3` exit 0, `"ok": true`, zero `open_errors`/`stuck_open_cycles`/`stuck_closed_cycles` — no hard-abort/reparent crash. **Separate from and does not cover** the process-not-reaped RAM finding in §2, which is a leak, not a crash. |
| 19 | Current QA gate green | ⏳ | Targeted gate for every file this task touched is green locally (`takkub qa-gate --targeted` over the four files in §1). **GitHub CI on `origin/main`'s last-pushed commit (`3166bb8`) is currently red** — `ci` workflow fails on `tests/test_file_watch_service.py::TestDebounce::test_rapid_changes_to_same_path_flush_once` (macOS-only flake). This is already fixed in the local (unpushed) `main` at commit `b5fd018` ("test(file-watch): debounce test bounded pump + stop timer (macOS CI flake, #365)") — local `main` has moved well past `origin/main` (phase 7 Capability Hub, a full WebEngine soak 25×3 audit, etc.) with none of it pushed/CI-verified yet. Real "is the gate green" answer needs a push + fresh CI run, which is outside this task's/role's scope (push is Lead's call). |

### Summary

- ✅ 13/19 fully verified in code + automated tests, no caveats.
- ⏳ 5/19 (items 2, 5, 6, 7, 8, 19 — six, not five) are code/unit-verified but need
  either a real browser/GUI session (items 2/5/6/7/8 — visual Monaco/diff/Preview
  rendering, none of which this role can produce here) or a fresh pushed CI run
  (item 19) to fully close out.
- ❌ 0/19 outright broken, but §2's RAM "≈0 after close" number is a real, measured
  gap flagged for follow-up — not swept under either the ✅ item 18 (crash-only) or
  any of the ⏳ rows above.
