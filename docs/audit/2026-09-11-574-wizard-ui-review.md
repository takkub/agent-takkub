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


## Round 2 — 69f81337

**Verdict: FAIL. wizard matches approved mockup: no**

Reviewed main/worktree `69f81337`, including frontend `ca9bbe23` and the real `boot_flow.py`. During the audit main advanced to `00205fbd` (dead legacy `boot_update_window.py` removal); `git diff 69f81337 00205fbd -- src/agent_takkub/boot_flow_window.py src/agent_takkub/boot_flow.py` is empty, so the audited wizard/backend code is still current. No application source or existing tests changed. The round-1 missing-module finding B2 is **closed as an import prerequisite**, but actual backend/UI interoperability fails below. Escape itself is fixed. This section supersedes the round-1 status for each V row.

### Method, evidence, and limits

Used the original `574-review-render.py` capture/metrics routine, offscreen **real PyQt6 widgets**, bundled fonts, Windows/Python 3.11, **96 DPI / DPR 1 / 640 × 540**, for A–E in both themes. Read approved PreMigrate/Migrating/Done/Failed HTML/CSS at the exact original design path. The expected values in the original table remain the specification. Light colors are checked against current light theme tokens, since no separate approved light HTML exists. All ten new images were opened and visually inspected.

Unlike the earlier fake flow, `BootFlowWindow()` resolves the actual imported `agent_takkub.boot_flow` module. Its check/plan/migration functions are patched at their external work boundary to return fake values constructed with the **actual frozen/slotted backend dataclasses**. QThread.start is **not patched**. Provider check and plan return through real workers; B→C uses the real migration worker, held by threading.Event; completion reaches D. No real user-data migration, provider update, or cockpit launch runs.

**Exact contract execution cannot render B–E:** `MigrationPlanSummary.backup_dir` is a `Path`, and the B slot passes it directly to `QLabel`, aborting the subprocess with TypeError. Therefore the complete ten-page set uses a clearly separate **path-normalized fixture**: only backup/archive/log paths are converted to strings. This is a diagnostic bypass in the artifact script, not a source fix or proof that production works. Four-tuple backup rows, numeric phases, and actual available dataclass fields are retained; no invented `verify_steps`/`current_path` is attached to make the screenshots pass. The exact dataclasses currently cannot accept those two requested fields. `MigrationOutcome.validated_steps`, `failed_step_index`, and `failed_step_total` do exist and are supplied as 11, 7, and 11.

ART retains its definition above. New artifacts:

| Evidence | Relative to ART |
| --- | --- |
| Exact-contract real-module/worker renderer (reproduces process failure at B) | `574-round2-review.py`, `574-round2-exact-contract.log` |
| Path-normalized renderer and real QThread/Escape probes | `574-round2-normalized.py`, `574-round2-normalized.log` |
| All ten images | `screenshots/boot-flow-review-round2/{A,B,C,D,E}-{dark,light}.png` |
| Rectangles, resolved px fonts, QSS, tokens, layout gaps, font metrics, wrapping | `screenshots/boot-flow-review-round2/widget-metrics.json` |
| Dataclass fields, real-thread identity, Escape, repeated phase, direct done results | `screenshots/boot-flow-review-round2/behavior.json` |
| Independently caught B/D/E Path and E log failures, baseline/log-gap measurements | `574-round2-contract-probes.py`, `574-round2-contract-probes.log`, `screenshots/boot-flow-review-round2/contract-probes.json` |
| Existing test run | `574-round2-pytest.log` |

Reproduce using the reviewed worktree as cwd: `python <ART>/574-review-run.py 60 python <ART>/574-round2-normalized.py`. Exact-contract reproduction: substitute `574-round2-review.py`; expect nonzero process exit. Probes: substitute `574-round2-contract-probes.py`. Commands have bounded subprocess deadlines (render 60s, contract 20–30s, tests 120s); worker waits/event-loop waits are bounded too.

### V1–V15 remeasurement — both dark and light

PASS below concerns the specified visual row in the diagnostic render; it does not waive the blocking Path integration failure. Values are the same in both themes unless colors are listed separately.

| ID / page / element | Expected from approved HTML | Round-2 actual measurement | Verdict |
| --- | --- | --- | --- |
| V1 — A–E typography | Title 16px/700; subtitle 12px/400; body/button 13px; chip/log 11px; percent 22px/700; inherited line-height 1.45 | All requested/resolved font **pixel sizes and weights now match**. But line rhythm is still native Qt: title rect `(20,20,600,20)` versus CSS line box **23.2px**; subtitle starts **y=44** versus **47.2** from 20 + 23.2 + 4. Header track starts **y=74** versus CSS **76.6**. Native 13px Latin line spacing is **17px**, versus CSS **18.85px**. No 1.45 equivalent is applied. | **FAIL** — size units fixed; line-height remains |
| V2 — A/B aggregate track | Visible 4px GROUND_INPUT at zero, above hairline | Both pages show `(0,74,640,4)` track; dark `#0F172A`, light `#F8FAFC`; 1px hairline below. | **PASS** |
| V3 — C header/aggregate | Migration subtitle phase 2/5; 4px aggregate at 34%; stable phase for repeated events in that phase | First backup then promote event yields **2/5**, **34%**, 4px track and 8px main bar. However another actual `ProgressEvent(phase=2, done=4, total=9, percent_overall=40)` changes header to **3/5** and activates verify. Numeric phase is treated as unknown, so event count determines phase. | **FAIL** — initial frame fixed; actual event contract fails |
| V4 — B/D/E key/value grid | 150px key + 12px gap, value x=196, aligned baselines | Keys **150px**, gap **12px**, values **x=196**. B first key/path widgets `(34,281,150,20)` / `(196,281,410,20)`. QTextLayout first-line baselines both **y=295** (Thai ascent 14; centered mono ascent 12 + 2px). Other shown rows visually align. | **PASS** for rendered sample |
| V5 — B section heading spacing | Heading-card gap 6px; tracking 0.04em = 0.48px | Heading y=91 h=18, card y=115: **6px**. QSS **0.48px**, resolved QFont **0.46875px** (Qt subpixel quantization). | **PASS** |
| V6 — B backup units | `15,747 ไฟล์`, `29 โปรเจค`, `15 ไฟล์`, `4 รายการ` | Actual `(label,count,bytes,unit)` fixtures render **`15,747 1200000000`**, **`29 29000`**, **`15 15000`**, **`4 4000`**. UI consumes tuple index 2 (bytes) as unit, ignoring index 3. | **FAIL** |
| V7 — B explanatory note | 16px info circle, 8px gap, 1px top offset; bold primary phrase, muted rest | Icon `(20,375,16,16)`, text `(44,375,576,36)`: size/gap pass; **top offset 0px**, expected icon y=376. Bold primary span and muted prose now render correctly. | **FAIL** — remaining 1px offset |
| V8 — C future-phase dots | Numbers 3/4/5, diameter 22px, text 11px/600 | Actual todo numbers **3,4,5**, all **22×22**; painter font **11px/600**; active 2 uses 700. Visible in both captures. | **PASS** |
| V9 — C counters | Verify 11 actual validation steps; active `3 / 9 รายการ · providers/…` in PRIMARY | Verify still **9 ขั้น**. Dataclass has **no verify_steps**, so UI falls back to 9 promoted categories. Active **`3 / 9 รายการ`** has correct PRIMARY (`#F3F4F6` / `#0F172A`), but **no path segment**, because actual ProgressEvent has **no current_path**. | **FAIL** |
| V10 — C log styling | 11px mono; faint timestamp, muted operation/detail, primary path; 8px gaps | Font now **11px IBM Plex Mono**. Actual backend-shaped `promote-v2-root: providers/codex/default` is put entirely into the first span, colored **FAINT** (`#4B5563` / `#94A3B8`); timestamp/operation/path separation absent. Parser requires double spaces while backend emits `step_id: name`. Even structured sample input uses two NBSPs measuring **14px**, not an 8px gap. | **FAIL** |
| V11 — C footer | Margins 14/20/18; warning x=20, 8px icon/text gap; complete wrapping backup note | Icon **x=20**, text **x=44**, gap **8px**; correct outer margins. Warning text advance **328px** fits its **340px** slot. Complete right note `(471,489,149,33)` wraps without ellipsis; prefix and timestamped path present. | **PASS** |
| V12 — D validation result | Heading includes 11 validation steps; exactly 3 summary rows | Actual MigrationOutcome(validated_steps=11) produces **ตรวจสอบครบ 11 ขั้น — ไม่มีข้อมูลหาย**; actual summary layout has **3 rows** in both themes. | **PASS** |
| V13 — D downgrade instructions | Muted Thai prose; inline command in 12px IBM Plex Mono | Value `(196,335,410,20)` is RichText, surrounding **13px sans/MUTED**, command span **12px IBM Plex Mono**. Optional previous-version suffix absent because backend outcome lacks previous_version and local fallback yields none; not hardcoded for this audit. | **PASS** for styling |
| V14 — D footer hint | FAINT prose and MUTED `Settings → Storage` | Actual RichText has base **#4B5563 / #94A3B8**, embedded span **#6B7280 / #64748B**, font **12px**. | **PASS** |
| V15 — E failure icon | 36px tinted disk, 20px outlined warning triangle + !, SVG-equivalent stroke 2.5 | Visible triangle + ! now inside **36×36** disk; drawing helper uses **20px** viewbox scale and stroke **2.5 × 20/24**. Correct dark/light disk/error tokens. Exact curved SVG corners versus Qt polygon rasterization remain the original N1 limitation. | **PASS** |

**Visual rows: 9 PASS, 6 FAIL (V1, V3, V6, V7, V9, V10).** These failures include small spacing differences because the approved mockup remains the specification. Chip pills remain correct, including **20px** height/radius **10px** and correct input-background tokens. No new missing-chip finding.

### B1 — actual Escape test passes; direct completion API remains unguarded

In **both themes**, after the actual module's patched migration callable entered a real QThread, `QTest.keyClick(w, Qt.Key.Key_Escape)` returned:

```json
{"visible": true, "finished": [], "worker_running": true}
```

The callable confirmed `QThread.currentThread() is not app.thread()`. Native `w.close()` returned false. Calling `accept()` and `reject()` also retained visibility and emitted nothing. Releasing the worker reached D. **The original user-key Escape bypass is fixed.** These probes need the explicitly documented path-normalized plan to reach C at this HEAD.

A separate held-worker probe of `w.done(0)` still returns `{"visible": false, "finished": [true], "worker_running": true}`. `done()` at lines 1716–1718 calls its superclass and emits unconditionally. This is a **remaining completion-API guard hole**, not a claim that Escape still reaches it or that a current user action invokes it. Guard the final completion boundary as requested in round 1; direct API reachability is demonstrated, production user-trigger reachability is not established.

### Remaining MUST-FIX — element / expected / actual

| Finding | Element / expected | Actual / cause / reference |
| --- | --- | --- |
| R2-B1 — blocking integration | B/D/E path labels and E log list must accept actual backend Path fields | Exact plan result aborts GUI subprocess at B; independently caught B/D/E label calls all throw WindowsPath TypeError. With backup path removed, E still throws joining `list[Path]`. `boot_flow_window.py:517,957,1374,1379,1543,1548`. Normalize at display boundaries; do not change fixtures to conceal it. |
| R2-B2 — phase integration | Repeated numeric phase 2 stays promote; failed numeric phase 3 with step 7/11 shows `ขั้นที่ 3 ตรวจสอบ (7/11) ไม่ผ่าน` | C advances to verify on the second phase-2 event. E renders **ขั้นตอน 3 ไม่ผ่าน**, losing both translated phase and 7/11 despite fields being supplied. GUI only recognizes string keys; actual backend uses ints. `boot_flow_window.py:1192–1207,1474–1496`. |
| R2-B3 — plan/progress interface | Correct four-tuple unit; real validation-step total and current path; compatible structured/color log | Bytes displayed as units; verify fallback 9; missing current path; complete backend log colored FAINT. See V6/V9/V10 and actual field inventory in behavior.json. `boot_flow_window.py:925–926,1152–1155,1218–1239`; real boot_flow dataclasses and emit/on_entry. Coordinate the actual shared contract rather than adding optional duck-typed fields only to tests. |
| R2-V1 — typography | CSS 1.45 line boxes | Native Qt title 20px instead of 23.2px; header/subtitle shifted upward. Font px conversion is fixed; line-height still is not. See V1. |
| R2-V7 — note icon | 1px top offset | Icon and note both y=375, delta 0. See V7; `boot_flow_window.py:878`. |
| R2-V10 — log separation | Exact 8px inline gaps | Two NBSPs measure 14px at the new 11px mono font. See V10, independently measured in contract-probes.json. |
| B1 residual — completion API | Final done boundary cannot signal proceed during C | Direct `done(0)` still dismisses and emits true while worker runs; Escape/accept/reject are fixed. `boot_flow_window.py:1716–1718`. Latent API hole, not demonstrated user-key bypass. |

### Verification result and acceptance

Existing tests **90 passed**, exit 0: `tests/test_boot_flow_window.py`, `tests/test_boot_main_window_gate.py`, `tests/test_boot_flow.py`, and `tests/test_boot_flow_terminal.py` (`-q` plus project quiet options omit the textual count summary; progress is 72 + 18). These tests do not invalidate the independently reproduced real-contract failures: the window tests explicitly use their own fake interface and synchronous QThread patch. This round did not repeat the prior event-flood benchmark; no new flood-performance claim is made.

All ten diagnostic renders and requested real-key Escape probes completed. Exact production-type integration was tested and fails before B can render; no claim of end-to-end migration success is made. Fix the remaining contract and visual differences, then rerun the exact-contract renderer without any path normalization.

**wizard matches approved mockup: no**
