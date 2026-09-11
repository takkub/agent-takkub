# #574 boot-flow wizard — UI review, 2026-09-11

**Verdict: FAIL — ยังไม่ตรง mockup และ Escape ข้าม migration gate ได้ขณะ worker ทำงาน.**

Reviewed `8dac99e3` (main / this worktree HEAD, includes frontend `c77451b9`). No application source or existing tests changed. This review uses the approved HTML as the specification, not the frontend screenshots as the specification.

## Scope and reproducible evidence

Approved files read directly, including inline CSS:

`C:/Users/monch/AppData/Local/Temp/claude/C--Users-monch-WebstormProjects-agent-takkub/8889290a-14a2-45e1-bfe3-59a89bf52d2f/scratchpad/boot-flow-design/{Main,PreMigrate,Migrating,Done,Failed}.dc.html`

Central artifact root, abbreviated **ART** below:

`C:/Users/monch/WebstormProjects/agent-takkub/runtime/exports/2026-09-11/agent-takkub/`

Independently rendered real `BootFlowWindow` widgets using offscreen QApplication, bundled IBM Plex fonts, Windows/Python 3.11/PyQt6, logical DPI **96**, device pixel ratio **1**. Every capture is **640 × 540**. Applied `theme.apply_variant()` before constructing each theme's dialog; waited for event processing/painting before capture. No source monkeypatch, no `_font` replacement, no manually repaired header. Fake flow/plan/outcome shapes are imported from the existing tests. C is reached by the real B start handler with a real QThread held until its screenshot is saved; D follows its real completion. A has all five approved providers; B has all four approved backup categories; D has all nine promoted categories and `validated_steps=11`; E includes both log paths and step 7/11.

The running package reports **2.0.8** at this HEAD. Its appearance instead of the mockup's example 2.1.0 is data substitution, not a request to hardcode 2.1.0. C retaining B's entire confirmation message is a separate defect.

| Page | New dark screenshot, relative to ART | New light screenshot, relative to ART |
| --- | --- | --- |
| A Main | `screenshots/boot-flow-review/A-dark.png` | `screenshots/boot-flow-review/A-light.png` |
| B PreMigrate | `screenshots/boot-flow-review/B-dark.png` | `screenshots/boot-flow-review/B-light.png` |
| C Migrating | `screenshots/boot-flow-review/C-dark.png` | `screenshots/boot-flow-review/C-light.png` |
| D Done | `screenshots/boot-flow-review/D-dark.png` | `screenshots/boot-flow-review/D-light.png` |
| E Failed | `screenshots/boot-flow-review/E-dark.png` | `screenshots/boot-flow-review/E-light.png` |

Other evidence under ART:

- `574-review-render.py`: standalone reproducible renderer; use the reviewed worktree as cwd.
- `screenshots/boot-flow-review/widget-metrics.json`: visible widget rectangles, point/pixel sizes, weight, QSS, margins, spacing, theme tokens, DPI, and pixel samples for all 10 captures.
- `574-review-behavior.py` and `574-review-behavior.json`: real-thread behavior probes, including Escape and event flood.
- `574-review-pytest.txt`: existing wizard/gate tests, **40 passed**.
- `574-review-run.py`: subprocess runner with explicit timeout. Example: `python <ART>/574-review-run.py 45 python <ART>/574-review-behavior.py`.

Also visually inspected all six supplied frontend images under `ART/screenshots/boot-flow/`: `A-provider-update-dark.png`, `A-provider-update-light.png`, `B-premigrate-confirm-dark.png`, `C-migrating-dark.png`, `D-done-dark.png`, `E-failed-dark.png`. Their two-provider/two-backup-item data did not exercise the fuller approved composition. The supplied C screenshot has a manually different header/progress presentation from the actual B→C transition reproduced here; it cannot establish that the transition works.

## MUST-FIX — visible differences from the approved HTML

Source references in this section are to `src/agent_takkub/boot_flow_window.py` at the reviewed HEAD. Each issue applies to both themes unless stated otherwise. The screenshot names below resolve through the table above.

| ID / page / element | Expected from HTML | Actual measured/rendered behavior | Source / evidence |
| --- | --- | --- | --- |
| V1 — A–E typography | Title **16px/700**, subtitle **12px/400**, body **13px**, button **13px**, chip **11px**, C percent **22px/700**, log **11px mono**; body line-height **1.45** | `_font` uses point sizes. At 96 DPI, title **16pt → 21px**, subtitle **12pt → 16px**, body/button **13pt → 17px**, chip/log **11pt → 15px**, percent **22pt → 29px**. Title widget is `(20,20,600,28)`; subtitle `(20,52,600,24)`. No explicit equivalent of HTML line-height 1.45. Text and vertical rhythm are visibly larger, buttons wider, and wrapping differs. | `_font` near line 169; usages 562/565, 879, 925, helpers 375–410. All screenshots; metrics contain both requested point size and QFontInfo pixel size. Fix units first, then remeasure wrapping/spacing. |
| V2 — A/B aggregate track | Always-visible **4px** `GROUND_INPUT` track above the 1px hairline, even at 0% | A and B pass `percent=None` and hide the entire bar. Visible 4px band is absent. | `_set_header` 604–609, provider completion 1461–1465, plan completion 1496–1501. A/B screenshots. |
| V3 — C header and aggregate progress | Migration subtitle: `กำลังย้ายข้อมูลเป็นโครงใหม่ (...) — ขั้นตอน 2 จาก 5`; visible **4px**, **34%** accent aggregate bar matching the main progress | Real B→C leaves B's `เวอร์ชัน ... ใช้โครงสร้างข้อมูลใหม่ — ต้องย้ายข้อมูลครั้งเดียวก่อนเปิดใช้งาน` subtitle; aggregate remains hidden, though the body reads **34%**. Progress events never update phase ordinal in the subtitle. | 860–862 calls `_show_page`, whose body at 1395–1396 only changes stack index; 1026–1069 updates numbers but not visibility/header. Both C captures and failed `C-header-and-aggregate` probe. |
| V4 — B/D/E key/value grid | Key width **150px**, gap **12px**, baseline alignment | Key width **170px**; labels aligned top. Unbordered info-box value starts **x=216** instead of **x=196** from 20 + 14 + 150 + 12. Longer values receive 20px less width; key/value baselines are not explicitly matched. | 417–423, 441–455, 463–477. B/D/E screenshots and metrics. Existing test intentionally locks 170; that test does not establish mockup fidelity. |
| V5 — B section heading spacing | Heading-to-backup-card gap **6px**, letter-spacing **0.04em** (0.48px at 12px) | Same outer layout gives heading-to-card gap **12px**; QSS uses **1px** letter spacing. Heading/card group has no inner 6px layout. | 749–762. B screenshots. |
| V6 — B backup count units | `15,747 ไฟล์`, `29 โปรเจค`, `15 ไฟล์`, `4 รายการ` | Every row appends **รายการ**: `15,747 รายการ`, `29 รายการ`, `15 รายการ`, `4 รายการ`. | 811–832, especially 826. B screenshots with all four mockup categories. |
| V7 — B explanatory note | **16px** circle-info icon, **8px** gap, 1px top offset; `คัดลอกก่อนเสมอ` bold and `TEXT_PRIMARY`; remaining text muted | Icon absent; entire sentence is one regular/muted label beginning at x=20. Highlight was explicitly removed. | 771–787. B screenshots. |
| V8 — C future-phase dots | Future circles contain **3**, **4**, **5**; diameter **22px**, text **11px/600** | All three future circles are blank. Active 2 and completed check render, but `set_state('todo')` clears the number. | 1003–1005, 1048–1057; `_PhaseDot.set_state` default number is empty. C screenshots. Diameter itself passes. |
| V9 — C phase counters | Verify **11 ขั้น** for this mockup (nine promoted categories are a distinct quantity); active counter `3 / 9 รายการ · providers/…` in **TEXT_PRIMARY** | Verify label is **9 ขั้น**, derived from `len(plan.promote_items)`; active counter remains **TEXT_FAINT** (`#4B5563` dark / `#94A3B8` light). Only `done / total unit` is rendered; no current-path segment is constructed. | 1008–1010, 917–919, 1059–1063. C screenshots. Do not fix by hardcoding 11: expose/use the actual validation-step count. |
| V10 — C log colors | **11px mono**; timestamp **TEXT_FAINT**, operation/explanation **TEXT_MUTED**, current path **TEXT_PRIMARY**, 8px inline gaps | Single **15px-equivalent** mono QLabel in `TEXT_MUTED`; timestamp and path lose their distinct colors. Full mockup log text clips at the right. The HTML also intentionally hides horizontal overflow; clipping alone is not a separate defect, but font size and color treatment are. | 924–932, 1064–1066. C screenshots. |
| V11 — C footer | Footer padding **14/20/18**, warning icon at left content edge x=20 and text separated by **8px**; complete backup note/path in a wrapping flex item (HTML has no ellipsis rule) | Warning icon at **x=32**, text at **x=68**; fixed 340px text slot and layout distribute space differently. Right note is limited to **190px**, unwrapped and left-elided: `…ate-2026-09-11-0832/`; `สำรองไว้ที่ backups/pre-migrate-` disappears. Full value exists only in tooltip. | 938–969, 972–982. C metrics and screenshots. Outer padding is correct; internal positioning/presentation is not. |
| V12 — D validation result placement | Heading `ตรวจสอบครบ 11 ขั้น — ไม่มีข้อมูลหาย`; **three** summary rows | Heading always says `ตรวจสอบครบทุกขั้น — ไม่มีข้อมูลหาย`; even when supplied `validated_steps=11`, implementation adds a **fourth** summary row `ตรวจสอบ / ครบ 11 ขั้น` instead. | 1145, 1182–1186. D screenshots populated with 11. |
| V13 — D downgrade instructions | Surrounding Thai **TEXT_MUTED**, command `takkub migrate restore-v1` **12px IBM Plex Mono** inside the sentence | Entire value is **13pt IBM Plex Sans** (17px equivalent), **TEXT_PRIMARY**. Both command font and surrounding text color differ. | 1204–1214. D screenshots. Confirmed frontend's own reported deviation; preserving wrapping does not waive the approved mixed-font requirement. |
| V14 — D footer hint | Hint **TEXT_FAINT**, embedded `Settings → Storage` **TEXT_MUTED** | Entire hint uses **TEXT_FAINT** with no span distinction. | 1134–1138 and 487–492. D screenshots. |
| V15 — E failure icon | **36px** tinted disk containing a **20px warning-triangle outline + !**, stroke 2.5, error color | 36px tinted disk contains only two painted strokes forming **!**; triangle is absent. | 1237; `_circle_icon` 1534–1540. E screenshots. Background token/diameter pass. |

These are visible spec differences, including the small V5/V14 differences; they are not downgraded to NIT merely because they are easy to miss. No hardcoded dynamic counts/versions should be introduced to make sample screenshots pass.

## MUST-FIX — behavior / integration

### B1 — Escape bypasses migration close guard (high)

With a real migration QThread held in progress, `w.close()` returns false and the dialog remains visible: that path passes. Immediately sending a real Qt Escape key event produces:

```json
{"visible": false, "flow_finished": [true], "worker_running": true}
```

`closeEvent` (1506–1510) guards only that event. QDialog's Escape/reject path reaches `done()` (1512–1514), which unconditionally emits `flowFinished(self._proceed)`, while `_proceed` defaults to true. The gate listener at 1558–1565 consequently has a path to quit its wait and construct the cockpit while migration is still active. The probe confirms dismissal/signal/worker state; it does not run a real destructive migration or launch the cockpit. Guard reject/Escape and other completion paths during C, and add a real-event regression test rather than only calling `closeEvent` directly.

Evidence: `ART/574-review-behavior.json`, `escape-close-guard` false. Existing test `TestMigratingPageCloseGuard` checks only a synthetic close event and misses this.

### B2 — default boot-flow backend cannot resolve at reviewed main HEAD

`src/agent_takkub/boot_flow.py` is absent at `8dac99e3`. `BootFlowWindow()._resolve_flow()` raises ImportError (`cannot import name 'boot_flow' from 'agent_takkub'`). `app._boot_main_window()` uses `run_boot_flow_gate(MainWindow)` with default flow when boot updates are enabled. Thus fake-flow tests/rendering do not establish that production wizard startup works at this HEAD. This is an integration prerequisite, distinct from the frontend's visual defects, and was reported to Lead immediately. Recheck after the backend lands; no claim that a real migration or rollback ran successfully is made here.

## PASS

### Chip question: pill exists — do not file a missing-background fix

The latest supplied A dark/light screenshots **already show a pill**. Both independent A renders confirm it. `_styled(chip, ...)` at 702–705 paints the correct background; it is not plain text accidentally stripped by the dialog-wide QLabel rule.

Measured dark update-chip rectangle: **(537,108,69,20)**. Of 1,380 pixels in that crop, **895** are exactly `GROUND_INPUT #0F172A`, while **64** are the surrounding card `#14171E`, consistent with rounded corners. The QSS is radius **10px**, padding **0 8px**, fixed height **20px**, color `STATE_WARN`. Latest chip has `STATE_OK`. Light uses `#F8FAFC` over `#F1F5F9`. Font sizing remains V1; background/radius/height themselves pass.

### Shared geometry and tokens

| Element | Expected / actual | Result and limits |
| --- | --- | --- |
| Dialog | 640 × 540 | PASS, all 10 image sizes and widget rectangles |
| Header padding / gap | left/right/top 20, bottom 12; gap 4 | PASS as layout values; typography and line positions fail V1 |
| Hairline | 1px `BORDER_CARD` | PASS; not a replacement for the missing 4px track on A/B/C |
| Provider/backup card | 1px `BORDER_CARD`, radius 8, `GROUND_INSET` | PASS; no phantom repeated borders around child labels |
| A provider rows | 10px vertical / 14px horizontal, gap 10, name column 110 | PASS as layout values; font growth changes resulting row height |
| B backup rows | 8px vertical / 14px horizontal | PASS, as specified by B HTML (not A's 10px) |
| Info boxes | 10px vertical / 14px horizontal, gap 6, radius 8, `GROUND_INPUT` | PASS for these declared values; key grid fails V4 |
| Checkbox | widget 16 × 16; radius 4; checked accent; unchecked input/control border | PASS nominal dimensions/colors; drawn pen/edge rasterization is not browser-identical |
| Buttons | fixed 36px, radius 6, padding 0 18; primary `ACCENT_GOLD`/white; secondary input/card-border | PASS declared dimensions/colors; fonts fail V1; HTML secondary border box convention merits N1 |
| Footer | margins left/right 20, top 14, bottom 18; border-top 1; button gap 10 | PASS common margins; C's internal layout fails V11 |
| C main bar | 8px high, radius 4, accent over input | PASS; 34% fill visible |
| C dots | widget 22 × 22; active accent, completed green, future input/control border | PASS dimensions/state fills; labels fail V8 |
| D/E result disks | 36 × 36 | PASS size; E glyph fails V15 |

### Light theme coverage

All five light pages were inspected, not just A. Main titles, Thai/Latin body text, key/value content, buttons and paths are visible with no tofu glyphs or dark card backgrounds left behind. There are **no separate approved light `.dc.html` files** in the supplied set: exact light color comparison is therefore against `cockpit_theme`'s light tokens, while layout/font comparison remains against the approved HTML.

| Token | Approved dark literal / actual dark | Current light token / actual light |
| --- | --- | --- |
| GROUND_PANEL | `#181B22` | `#FFFFFF` |
| GROUND_INSET | `#14171E` | `#F1F5F9` |
| GROUND_INPUT | `#0F172A` | `#F8FAFC` |
| BORDER_CARD / ROW / CONTROL | `#262B36` / `#1F232C` / `#3A4150` | `#E2E8F0` / `#EDF1F6` / `#CBD5E1` |
| TEXT_PRIMARY / ALT | `#F3F4F6` / `#F8FAFC` | `#0F172A` / `#1E293B` |
| TEXT_MUTED / FAINT | `#6B7280` / `#4B5563` | `#64748B` / `#94A3B8` |
| ACCENT_GOLD | `#6366F1` | `#4F46E5` |
| STATE_OK / WARN | `#10B981` / `#F59E0B` | `#059669` / `#D97706` |
| STATE_ERROR_BRIGHT / ICON_BG | `#f87171` / `#3a2226` | `#dc2626` / `#fee2e2` |

Token values match at construction. Token *selection* still fails where documented (e.g. C active count uses FAINT, D restore prose uses PRIMARY). Readability is not an unconditional PASS: C's active count is unnecessarily faint and its backup path loses content; these are V9/V11. Shared faint hints/chip status contrast is N2. Runtime theme switching within an already-open wizard was not requested or exercised.

### Behavior checks that pass

- **40 existing tests passed:** `tests/test_boot_flow_window.py` + `tests/test_boot_main_window_gate.py`; offscreen, local `PYTHONPATH=src`, 110s inner test deadline / 120s outer deadline. Existing wizard tests patch QThread.start synchronously, so the independent probes below supplement them.
- Real QThreads: remembered **skip**, **update_all**, **selected** all reach B without populating A for selection. Skip invokes no updates; update_all/selected invoke only the selected update (`claude` in the test data). This verifies resulting selection/flow, not absence of a transient empty initial stack page during asynchronous checking.
- `TAKKUB_BOOT_UPDATE=0` on `BootFlowWindow.start()` goes to migration confirmation B and invokes no provider updates. At the **app entry point**, the flag instead bypasses the whole wizard and calls `_run_auto_migrate_headless()` before MainWindow (`app.py:973–981`). Both paths skip A; do not describe the application path as necessarily displaying B.
- Native close-event path during C is ignored; Escape does **not** pass (B1).
- Real queued-event stress: **263,000 progress events**, **2.015s**, **198** 10ms GUI heartbeat callbacks, longest observed gap **31.0ms**, successful D transition. `_on_progress` stores the latest event and a 120ms timer applies it. This bounded workload did not freeze the UI; it is not proof against an unbounded producer or large real I/O payloads.

## NIT / limitations

1. **N1 — raster/box-model parity:** Qt antialiasing, pen-center placement, native secondary-button border inclusion (QPushButton total 36px versus HTML `height:36px` plus a border without an explicit box-sizing rule), and browser line metrics require a final side-by-side browser render after the known unit/layout failures are fixed. This review does not claim pixel-diff equality from matching QSS literals. Approved HTML was read directly; no approved browser screenshot was supplied or generated. Actual Qt images were independently rendered.
2. **N2 — shared light contrast:** `TEXT_FAINT #94A3B8` hints and amber/green small chip text are noticeably pale on the light grounds. They use the current theme tokens, so this is not a wizard-specific palette mismatch. Consider a shared-theme readability follow-up; do not silently substitute dark tokens. No accessibility certification is claimed.
3. Dynamic content counts/version/backup names legitimately differ from the mockup when inputs differ. The audit deliberately supplies the mockup's five providers/four categories/nine promoted categories/11 validation steps to distinguish missing presentation from missing sample data. The extra validated row is V12, not merely a data difference.
4. The offscreen plugin logs that its generic `Qt6/lib/fonts` directory is absent. `ensure_fonts_loaded()` reports bundled IBM Plex and Noto Sans Thai successfully loaded, and the inspected images show readable Thai/Latin text; this warning did not prevent captures/tests.

## Required follow-up before acceptance

Fix V1–V15, close all migration dismissal paths (B1), and integrate the missing backend (B2). Add focused regressions for B→C header/bar and Escape with an active worker. Then rerender A–E in both themes through real transitions with the same full fixture data. Keep the chip pill implementation: its background is already correct. Production data migration/rollback acceptance remains outside this fake-flow UI review.
