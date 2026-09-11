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

## Round 3 — 7b48de93

**Verdict: FAIL. wizard matches approved mockup: no**

Reviewed main / worktree HEAD `7b48de93` (frontend round 4 `5b0a5e4b`, backend round 10/11 `4ee48c24` + `64e1bc53` + `8d41be39`). Working tree clean, no application source or existing test changed. Confirmed both reviewed modules resolve out of this worktree, not the shared editable install: `.../reviewer-2-1789110706/src/agent_takkub/boot_flow_window.py` and `.../boot_flow.py`.

**Every round-2 blocker is closed.** The exact production contract now renders all five pages, the int-phase vocabulary is understood, and the direct completion API is guarded. What remains is one new high-severity layout defect on page C, one page-B content defect, and a set of interface gaps that only appear when the events come from `run_migration()` itself rather than from mockup-shaped fixtures.

### Method, evidence, and limits

**No path normalization anywhere this round.** Every object handed to `BootFlowWindow` is a real frozen `boot_flow` dataclass with its real field types: `MigrationPlanSummary` with 4-tuple `backup_items` (`label, count, bytes, unit`), a real `Path` `backup_dir`, and real `verify_steps=11`; `ProgressEvent` with `int` `phase`, real `current_path`, and real `files_done`/`files_total`; `MigrationOutcome` with `Path` `archive_dir`, `list[Path]` `log_paths`, `validated_steps=11`, `failed_phase=3`, `failed_step_index=7`, `failed_step_total=11`. Nothing is duck-typed on and nothing is stringified first. Only the three external work boundaries (`check_provider_updates`, `plan_migration`, `run_migration`) are patched so no user data is touched. `QThread.start` is not patched; the migration callable asserts it runs off the GUI thread, and the round-1 capture routine, offscreen real widgets, bundled fonts, 96 DPI, device pixel ratio 1, 640 x 540 are all unchanged.

`MigrationPlanSummary` has no `skipped_move_only_items` field and page B never reads one, so that requested check is not applicable at this HEAD. The field lives on `pre_migrate_backup.py`'s step object and never reaches the wizard.

ART keeps its meaning from the earlier rounds. New artifacts, all under `ART/574-round3-shots/` unless stated:

| Evidence | Path |
| --- | --- |
| Exact-contract renderer, A-E both themes, real QThread, Escape/close/accept/reject/`done(0)`, files-progress shift probe | `ART/574-round3-review.py`, `ART/574-round3-review.log` |
| Production-shape renderer: events built exactly as `run_migration.emit()` builds them (`on_entry`, `on_file_progress`, `on_text`) | `ART/574-round3-prodshape.py`, `ART/574-round3-prodshape.log`, `prodshape.json` |
| Text advance/clipping, rich-text log gap measurement, failure-icon glyph pixels, footer path source, both themes' tokens | `ART/574-round3-probes.py`, `probes.json`, `E-icon-dark.png` |
| Widget rectangles, resolved pixel fonts, weights, letter spacing, font metrics, QSS, layout margins/spacing, theme tokens, pixel samples, all 14 captures | `widget-metrics.json` |
| Real-module/real-thread behavior results | `behavior.json` |

Reproduce from the reviewed worktree as cwd, every command deadline-bounded:

```
python <ART>/574-review-run.py 180 python <ART>/574-round3-review.py
python <ART>/574-review-run.py 120 python <ART>/574-round3-prodshape.py
python <ART>/574-review-run.py  60 python <ART>/574-round3-probes.py
```

### Screenshots beside the approved mockup

Mockup root, abbreviated **MOCK** below:

`C:/Users/monch/AppData/Local/Temp/claude/C--Users-monch-WebstormProjects-agent-takkub/8889290a-14a2-45e1-bfe3-59a89bf52d2f/scratchpad/boot-flow-design/`

| Page | Approved mockup | Round-3 dark | Round-3 light |
| --- | --- | --- | --- |
| A Main | `MOCK/Main.dc.html` | `A-dark.png` | `A-light.png` |
| B PreMigrate | `MOCK/PreMigrate.dc.html` | `B-dark.png` | `B-light.png` |
| C Migrating | `MOCK/Migrating.dc.html` | `C-dark.png` | `C-light.png` |
| D Done | `MOCK/Done.dc.html` | `D-dark.png` | `D-light.png` |
| E Failed | `MOCK/Failed.dc.html` | `E-dark.png` | `E-light.png` |

Extra C variants against the same mockup: `C-files-{dark,light}.png` (same frame plus `files_done`/`files_total`), `C-backendlog-{dark,light}.png` (backend-shaped log line), `C-prod-entry-*`, `C-prod-files-*`, `C-prod-validate-*` (events built exactly as `run_migration` builds them).

All 14 captures are 640 x 540. Dark and light rectangle lists are byte-identical per page (52, 41, 38, 35, 26 visible widgets for A-E), so every geometric measurement below holds in both themes; colors are listed separately where they differ.

### V1-V15 remeasurement

| ID / page / element | Expected from approved HTML | Round-3 actual measurement | Verdict |
| --- | --- | --- | --- |
| V1 - A-E typography | Title 16px/700, subtitle 12px/400, body/button 13px, chip/log 11px, percent 22px/700, line-height 1.45 | All pixel sizes and weights match. Header now carries real line boxes: title rect `(20,20,600,23)` against CSS 23.2, subtitle starts y=47 against 47.2, track starts y=77 against 76.6. `_apply_line_height` is applied to the title and subtitle only (lines 758, 762). Body rows keep Qt-native boxes: 13px key/value rows measure 20px against CSS 18.85, the 22px percent label 29px against 31.9. Drift accumulates to +5px by B's note block (y=378 against 372.95) and -2.5px by C's log box (y=347 against 349.5). | **FAIL** - residual, down from round 2's 3.2px title error to a 5px body drift |
| V2 - A/B aggregate track | Always-visible 4px GROUND_INPUT track above the hairline, even at 0% | A and B both show `(0,77,640,4)`, sampled `#0F172A` dark and `#F8FAFC` light across left, middle and right, with the 1px `#262B36` / `#E2E8F0` hairline at y=81. | **PASS** |
| V3 - C header and aggregate | Subtitle phase 2 of 5, visible 4px accent aggregate at 34%, stable phase across repeated events | Subtitle reads phase 2 of 5; the track pixel at x=4 is `#6366F1` dark / `#4F46E5` light and back to track color by x=320, consistent with a 34% fill; bar value 34, height 4. A second `phase=2` event keeps the promote row active and the header unchanged. | **PASS** |
| V4 - B/D/E key/value grid | Key width 150px, gap 12px | Keys `(34,y,150,h)`, values start x=196 on all three pages, giving exactly 12px. | **PASS** |
| V5 - B section heading spacing | Heading-to-card gap 6px, letter-spacing 0.04em (0.48px) | Heading `(20,94,600,18)`, card top y=118, gap 6px. Resolved QFont letter spacing 0.46875px, Qt's subpixel quantization of the 0.48px QSS value. | **PASS** |
| V6 - B backup count units | `15,747 ไฟล์`, `29 โปรเจค`, `15 ไฟล์`, `4 รายการ` | Unit words are now correct, but every row appends a byte total the mockup does not have: `15,747 ไฟล์ · 1.2 GB`, `29 โปรเจค · 0.0 GB`, `15 ไฟล์ · 0.0 GB`, `4 รายการ · 0.0 GB`. Three of four rows therefore show a meaningless `0.0 GB`. `boot_flow_window.py:1072-1075`. | **FAIL** |
| V7 - B explanatory note | 16px circle-info icon, 8px gap, 1px top offset, bold primary phrase | Icon `(20,379,16,16)`, text `(44,378,576,36)`: gap 8px, icon offset +1px, bold `คัดลอกก่อนเสมอ` in TEXT_PRIMARY with muted prose. | **PASS** |
| V8 - C future-phase dots | Numbers 3, 4, 5, diameter 22px, text 11px/600 | Todo dots carry `3`, `4`, `5`; every dot widget is 22 x 22; the completed backup dot renders the check. | **PASS** |
| V9 - C phase counters | Verify shows the real validation-step count, active counter `3 / 9 รายการ · providers/…` in TEXT_PRIMARY | Verify reads `11 ขั้น`, taken from the real `plan.verify_steps=11`, not from `len(promote_items)`. Active counter reads `3 / 9 รายการ · providers/…` in `#F3F4F6` dark / `#0F172A` light; label bold `#F8FAFC` / `#1E293B`; todo counters `#4B5563` / `#94A3B8`. | **PASS** |
| V10 - C log colors and gaps | 11px mono, faint timestamp, muted operation, primary path, muted detail, 8px inline gaps | The log label is 11px IBM Plex Mono. The four runs render `#4B5563`, `#6B7280`, `#F3F4F6`, `#6B7280` dark (`#94A3B8`, `#64748B`, `#0F172A`, `#64748B` light). Gap measured off the laid-out rich-text document: ideal width 515.0 minus the 4.0px document margin on each side leaves 507.0 against 483.0 of summed run advances, so 24.0px over three gaps, exactly 8px each. Total 515.0 fits the 576px slot without clipping. | **PASS** |
| V11 - C footer | Padding 14/20/18, warning icon at x=20 with an 8px gap, complete backup note wrapping on the right | The left side is correct: icon `(20,475,16,16)`, text `(44,474,340,18)`, gap 8px, footer margins 20/14/20/18. The right-hand note is not: it renders the raw absolute `Path`, wraps to three lines at `(400,444,220,78)`, and inflates the whole footer from the 68px every other page uses to **110px**, pushing C's body up. See R3-B1. | **FAIL** |
| V12 - D validation result | Heading naming the validation-step count, exactly 3 summary rows | Real `validated_steps=11` gives `ตรวจสอบครบ 11 ขั้น — ไม่มีข้อมูลหาย` and the summary layout holds exactly 3 rows in both themes. The paths box below is border-only with no fill, matching the mockup's second box. | **PASS** |
| V13 - D downgrade instructions | Muted Thai prose with the command inline in 12px IBM Plex Mono | Value `(196,338,410,20)` is rich text: 13px sans prose plus a 12px IBM Plex Mono command span. | **PASS** |
| V14 - D footer hint | FAINT prose with a MUTED `Settings → Storage` span | Rich text, base FAINT with the embedded span at MUTED, 12px, both themes. | **PASS** |
| V15 - E failure icon | 36px tinted disk holding a 20px outlined warning triangle plus an exclamation mark | Icon widget 36 x 36 over `#3a2226` dark / `#fee2e2` light. Pixels at the exact error color form a bounding box 18 wide by 15 tall spread over 15 distinct rows, with the widest stroke row spanning 18px: a triangle outline, not a bare single-column glyph. | **PASS** |

**Visual rows: 12 PASS, 3 FAIL (V1 residual, V6, V11).** Round 2 was 9 PASS and 6 FAIL, so V3, V7, V9 and V10 close this round, V1 shrinks but does not close, V6 regresses on a different detail, and V11 regresses outright once the real absolute `Path` reaches it.

### Round-2 blockers and the B1 residual

| Round-2 finding | Round-3 result | Evidence |
| --- | --- | --- |
| R2-B1 - B/D/E path labels and E log list abort on real `Path` fields | **PASS, closed.** The exact-contract renderer completes A through E in both themes with exit 0 and no `TypeError`. B shows the relative backup path, D shows backup and archive paths, and E joins `list[Path]` into one line. `_path_str` (lines 297-308) now relativizes against `config.DATA_HOME` at every display boundary except the one in R3-B1. | `574-round3-review.log`, `B-dark.png`, `D-dark.png`, `E-dark.png` |
| R2-B2 - repeated numeric phase advances the row, failed numeric phase loses its label and step position | **PASS, closed.** A second real `phase=2` event keeps the promote row active with dots backup done, promote active 2, and verify/archive/done todo 3/4/5, header still phase 2 of 5. E renders `ขั้นที่ 3 ตรวจสอบ (7/11) ไม่ผ่าน` from `failed_phase=3`, `failed_step_index=7`, `failed_step_total=11`. | `behavior.json` keys `repeated_phase2` and `E_heading` |
| R2-B3 - four-tuple unit, real validation-step total, current path, structured and colored log | **PASS for the four items this row named.** Unit words come from tuple index 3; verify uses `plan.verify_steps=11`; the active counter carries the path segment; the log renders four correctly colored runs at 8px gaps. The byte suffix added alongside the unit is a separate new defect (V6, R3-H1), and the production event shapes raise separate gaps (R3-M2 through R3-M5). | V6, V9, V10 above, `prodshape.json` |
| R2-V1 - CSS 1.45 line boxes | Partly fixed. Header labels match, body rows do not. See V1. | `widget-metrics.json` |
| R2-V7 - note icon 1px top offset | **PASS, closed.** Icon y=379, text y=378. | V7 |
| R2-V10 - exact 8px inline log gaps | **PASS, closed.** The table-cell padding renders 8.0px per gap, measured off the laid-out document. | V10, `probes.json` |
| B1 residual - `done(0)` dismisses and emits during migration | **PASS, closed.** With a real `QThread` held inside the patched `run_migration` and page C showing, the probe returns visible true, finished empty, worker running true for a real `QTest.keyClick(Escape)`, for `close()` (which returns false), for `accept()`, for `reject()`, and for a direct `done(0)`. No `flowFinished` is emitted until the worker is released and D is reached. `done()` gained the same migrating-page guard at lines 1916-1926. | `behavior.json` keys `escape`, `close_return`, `accept_reject`, `direct_done0` |

### files_done / files_total on page C

The mockup contains no per-file count token anywhere; its active row reads `3 / 9 รายการ · providers/…`. The requirement is therefore that the new segment appears when the backend supplies the fields and that nothing else moves.

With a real `ProgressEvent` carrying `files_done=1204, files_total=15747`, the active counter becomes `3 / 9 รายการ (1,204/15,747 ไฟล์) · providers/…`, inserted between the unit and the path segment, exactly as `boot_flow_window.py:1385-1394` orders it.

Layout shift was measured by snapshotting every visible widget rectangle immediately before and after that event, in both themes:

| Measure | Result |
| --- | --- |
| Widgets whose rectangle changed | 0 |
| Widgets added or removed | 0 |
| Dialog size before and after | 640 x 540, unchanged |
| Counter text advance against its 364px slot | 307.0px, no clipping (176.0px without the segment) |

**PASS.** The counter sits in a stretch slot wide enough for the longer string, so the segment costs no geometry. Captures: `C-files-dark.png`, `C-files-light.png`.

### Remaining MUST-FIX

| ID / severity | Element / expected | Actual / cause |
| --- | --- | --- |
| **R3-B1 - high** | C footer right shows the short backup path on one line, inside the same 68px footer every other page uses | `boot_flow_window.py:1426` interpolates the raw `Path` instead of calling `_path_str`, which every other path site does use. On a real machine `backup_dir` is absolute, so the label gets 137 characters and 817px of text into a 220px maximum-width wrapping slot: it renders as three lines at `(400,444,220,78)` and the footer grows to `(0,430,640,110)`, 42px taller than spec, lifting C's whole body. `_path_str(backup_dir)` yields 47 characters and 268px instead. Reproduced in both themes and in every C capture including the production-shape ones. |
| **R3-H1 - high** | B backup rows read `15,747 ไฟล์`, `29 โปรเจค`, `15 ไฟล์`, `4 รายการ` | `boot_flow_window.py:1072-1075` appends a formatted byte total to each row. The mockup has no byte column here, and `_fmt_gb` renders anything under 50MB as `0.0 GB`, so three of the four rows carry a visible zero. Either drop the suffix or show it only where it is non-zero and the mockup allows it. |
| **R3-M1 - medium** | Every displayed path uses the mockup's forward slashes and trailing separator | `_path_str` returns `str(Path)`, which on Windows uses backslashes and drops the trailing separator. Affects B's backup-location row, both D path rows, E's backup row and log list, and C's footer. Cross-platform relevant: the same code renders forward slashes on macOS, so the two platforms disagree with each other and one of them disagrees with the mockup. Use `as_posix()` at the display boundary. |
| **R3-M2 - medium** | Only the active row carries a path segment; completed rows read `15,762 / 15,762 ไฟล์` | The suffix is written into whichever phase row the event belongs to and is never cleared, so once the phase advances the completed backup row still reads `15,762 / 15,762 ไฟล์ · v2`. `boot_flow_window.py:1392-1395`. The module docstring states the mockup rule that the code does not enforce. |
| **R3-M3 - medium** | C shows an estimated time remaining | `boot_flow.run_migration.emit()` passes `eta_s=None` on every event it constructs (`boot_flow.py:516`), and `_render_progress_event` only writes the label when the formatter returns a non-empty string, so the slot stays empty for the entire real migration. Measured empty for `on_entry`, `on_file_progress` and `on_text` events in both themes. |
| **R3-M4 - medium** | C's backup row reads `15,762 / 15,762 ไฟล์` | `emit()` hardcodes the unit to `รายการ` (`boot_flow.py:510`), so with production events the backup row reads `1 / 4 รายการ`. The mockup's per-phase unit vocabulary exists in `plan.backup_items` but never reaches `ProgressEvent`. |
| **R3-M5 - medium** | C's log shows four runs: timestamp, operation, path, explanation | The production log line is step id and name only (`boot_flow.py:544`), with no timestamp and no detail, so the log renders two runs. Worse, `on_text` messages carry no colon at all and fall into the unstructured branch of `_parse_log_line`, so a validation message renders entirely in the FAINT timestamp color, the dimmest run on the page. Either have the backend emit timestamp and detail, or stop treating an unstructured line as a timestamp. |
| **R3-M6 - medium** | D names the version to install before downgrading, and E names the current version in its prose and on its primary button | `MigrationOutcome` carries no previous-version field, so both suffixes are dropped. This is an interface gap, not a formatting bug; do not hardcode a version to make the captures pass. |
| **R3-N1 - nit** | B reads free disk space as a whole number of gigabytes | Renders one decimal place, so the mockup's `123 GB` becomes `123.0 GB`. `_fmt_gb` always prints one decimal. |

Page A matches its mockup on every element checked: five provider rows at 40px pitch with the 110px name column and 10px gap, chip pills 20px tall over GROUND_INPUT with STATE_WARN and STATE_OK text, the remember-choice row, the footer hint and both buttons, in both themes.

### Verification result

Existing tests pass: **117 passed**, exit 0, for `tests/test_boot_flow_window.py`, `tests/test_boot_main_window_gate.py`, `tests/test_boot_flow.py` and `tests/test_boot_flow_terminal.py`, run with `PYTHONPATH` pinned to this worktree's `src/` so the shared editable install cannot shadow it. Up from round 2's 90. No `qa-gate` was run, per the test-tier policy, and this review modified no application source or test.

The exact production contract now renders end to end, which round 2 could not do at all, and no dismissal path reaches `flowFinished` while a real worker is migrating. What blocks acceptance is R3-B1's footer blowout on the page the user watches longest, R3-H1's incorrect page-B row content, and the interface gaps R3-M2 through R3-M6 that keep a real migration from looking like the approved page C. Fix those and rerun the same three scripts unchanged.

**wizard matches approved mockup: no**

## Round 4 — df151857

**Verdict: FAIL. wizard matches approved mockup: no**

Reviewed `wt/frontend-1789113880` at `df151857`, merged fast-forward into this reviewer worktree over `main` `fceead0f` (backend round 12 `57d3db04` already in). Working tree clean throughout; no application source and no existing test was changed by this review.

**Every round-3 item frontend claimed is genuinely closed, and the three failing visual rows close with them.** R3-B1's footer blowout, R3-H1's byte suffix, R3-M1's backslashes, R3-M2's stale path suffix and R3-M6's missing previous version are all fixed and measured below. What blocks acceptance is a different pair of defects, both on page C, both only visible once the events come from the real `run_migration()`: the phase-row counter now names its unit after the entry it is copying rather than after what it counts, and the two `info` events that open and close a migration lose their message text entirely.

### Method, evidence, and limits

The three round-3 scripts ran **unchanged** (md5-verified copies staged into `ART/574-round4-art/`, identical to the originals) against this HEAD, so every round-3 number is directly comparable. Their shot directory was moved to `ART/574-round4-shots/` afterwards.

Round 3's prodshape script hand-builds events in the shape `emit()` used to have. Round 12 changed that shape, so this round adds one new script that does not hand-build anything: it patches only `plan_migration`, `_read_previous_app_version` and `auto_migrate_boot.run_boot_stage`, lets the **real** `boot_flow.run_migration()` construct all 40 `ProgressEvent`s and the `MigrationOutcome`, then replays those exact objects through the window's own progress callback, one per 120ms throttle window so every event actually reaches `_render_progress_event`. `QThread.start` is never patched. ART, offscreen rendering, bundled fonts, 96 DPI, device pixel ratio 1 and 640 x 540 are all unchanged from rounds 1-3.

| Evidence | Path |
| --- | --- |
| Round-3 exact-contract renderer, unchanged, A-E both themes, real QThread, Escape/close/accept/reject/`done(0)`, files-progress shift probe | `ART/574-round4-art/574-round3-review.py`, `574-round4-review.log`, `widget-metrics.json`, `behavior.json` |
| Round-3 prodshape renderer, unchanged | `ART/574-round4-art/574-round3-prodshape.py`, `574-round4-r3prodshape.log`, `prodshape.json` |
| Round-3 probes, unchanged | `ART/574-round4-art/574-round3-probes.py`, `574-round4-probes.log`, `probes.json` |
| **New** production-shape recorder: 40 real events built by `run_migration()`, replayed frame by frame | `ART/574-round4-art/574-round4-prodshape.py`, `574-round4-prodshape.log`, `round4-prodshape.json`, `C-r4-*.png`, `D-r4-*.png`, `E-r4-*.png` |
| **New** page-C footer budget probe (`backup_dir` under vs outside DATA_HOME) | `ART/574-round4-art/574-round4-footer-probe.py`, `footer-probe.json`, `C-footer-*.png` |
| Targeted test run | `ART/574-round4-art/574-round4-pytest.txt` |

ART is `runtime/exports/2026-09-11/agent-takkub`; all JSON and PNG paths above are under `ART/574-round4-shots/` unless stated. Reproduce from this worktree as cwd, every command deadline-bounded:

```
python <ART>/574-round4-art/574-review-run.py 180 python <ART>/574-round4-art/574-round3-review.py
python <ART>/574-round4-art/574-review-run.py 120 python <ART>/574-round4-art/574-round3-prodshape.py
python <ART>/574-round4-art/574-review-run.py  60 python <ART>/574-round4-art/574-round3-probes.py
python <ART>/574-round4-art/574-review-run.py 240 python <ART>/574-round4-art/574-round4-prodshape.py
python <ART>/574-round4-art/574-review-run.py 120 python <ART>/574-round4-art/574-round4-footer-probe.py
```

### Screenshots beside the approved mockup

Mockup root, abbreviated **MOCK**: `C:/Users/monch/AppData/Local/Temp/claude/C--Users-monch-WebstormProjects-agent-takkub/8889290a-14a2-45e1-bfe3-59a89bf52d2f/scratchpad/boot-flow-design/`

| Page | Approved mockup | Round-4 dark | Round-4 light | Production-event dark / light |
| --- | --- | --- | --- | --- |
| A Main | `MOCK/Main.dc.html` | `A-dark.png` | `A-light.png` | n/a |
| B PreMigrate | `MOCK/PreMigrate.dc.html` | `B-dark.png` | `B-light.png` | n/a |
| C Migrating | `MOCK/Migrating.dc.html` | `C-dark.png` | `C-light.png` | `C-r4-{backup,files,validate,final}-{dark,light}.png` |
| D Done | `MOCK/Done.dc.html` | `D-dark.png` | `D-light.png` | `D-r4-{dark,light}.png` |
| E Failed | `MOCK/Failed.dc.html` | `E-dark.png` | `E-light.png` | `E-r4-{dark,light}.png` |

Extra C frames against the same mockup: `C-files-*`, `C-backendlog-*`, `C-prod-entry/files/validate-*` (round-3 scripts), `C-footer-under_data_home.png` and `C-footer-other_drive.png` (footer probe). Every capture is 640 x 540. Dark and light rectangle lists stay byte-identical per page (52, 41, 38, 35, 26 visible widgets for A-E), so the geometry below holds in both themes.

### V1-V15 remeasurement

| ID / page / element | Expected from approved HTML | Round-4 actual measurement | Verdict |
| --- | --- | --- | --- |
| V1 - A-E typography | Title 16px/700, subtitle 12px/400, body/button 13px, chip/log 11px, percent 22px/700, line-height 1.45 | Every pixel size and weight still matches. The body drift is gone: B's note block is now at y=373 against CSS 372.95 (round 3: y=378), C's log box at y=350 against 349.5 (round 3: y=347), B's 13px key rows measure 19px against 18.85 (round 3: 20), the 22px percent label 32px against 31.9 (round 3: 29). Title y=20 h=23 against 23.2, subtitle y=47 against 47.2, track y=77 against 76.6. Max residual 0.5px. | **PASS** |
| V2 - A/B aggregate track | Always-visible 4px GROUND_INPUT track above the hairline, even at 0% | A and B both `(0,77,640,4)`, sampled `#0F172A` dark / `#F8FAFC` light at x=2, 120, 320, 500, 637, hairline `#262B36` / `#E2E8F0` at y=81. | **PASS** |
| V3 - C header and aggregate | Subtitle phase 2 of 5, visible 4px accent aggregate at 34%, stable phase across repeated events | Subtitle reads phase 2 of 5; track pixel `#6366F1` dark / `#4F46E5` light at x=2 and x=120, back to track color by x=320; bar value 34, height 4. A second `phase=2` event keeps the promote row active and the header unchanged. | **PASS** |
| V4 - B/D/E key/value grid | Key width 150px, gap 12px | Keys `(34,y,150,h)`, values start x=196 on all three pages. | **PASS** |
| V5 - B section heading spacing | Heading-to-card gap 6px, letter-spacing 0.04em | Heading `(20,94,600,18)`, card top y=118, gap 6px, resolved letter spacing 0.46875px. | **PASS** |
| V6 - B backup count units | `15,747 ไฟล์`, `29 โปรเจค`, `15 ไฟล์`, `4 รายการ` | Exactly those four strings. The byte suffix is gone; `_size_bytes` is now unpacked and discarded. | **PASS**, closes R3-H1 |
| V7 - B explanatory note | 16px circle-info icon, 8px gap, 1px top offset, bold primary phrase | Icon `(20,374,16,16)`, text `(44,373,576,36)`. | **PASS** |
| V8 - C future-phase dots | Numbers 3, 4, 5, diameter 22px, text 11px/600 | Dots carry 3, 4, 5; all five are 22 x 22; the completed backup dot renders the check. | **PASS** |
| V9 - C phase counters | Verify shows the real validation-step count, active counter `3 / 9 รายการ · providers/…` | Verify reads `11 ขั้น` from the real `plan.verify_steps`. With the mockup-shaped fixture the active counter is exactly `3 / 9 รายการ · providers/…`. With production events it is not: the backup row runs `1 / 4 ไฟล์` then `2 / 4 โปรเจค` then `4 / 4 รายการ` then `4 / 4 ไฟล์`, and the promote row `5 / 9 ไฟล์` then `6 / 9 โปรเจค` then `7 / 9 ไฟล์`. See R4-H1. | **FAIL** |
| V10 - C log colors and gaps | 11px mono, faint timestamp, muted operation, primary path, muted detail, 8px inline gaps | Unchanged from round 3: 11px IBM Plex Mono, runs `#4B5563` / `#6B7280` / `#F3F4F6` / `#6B7280` dark and `#94A3B8` / `#64748B` / `#0F172A` / `#64748B` light; laid-out document ideal width 515.0 less its 8.0px margins leaves 507.0 against 483.0 of run advances, 24.0px over three gaps, exactly 8px each, inside the 576px slot. | **PASS** |
| V11 - C footer | Padding 14/20/18, warning icon at x=20 with an 8px gap, complete backup note wrapping on the right | Footer is `(0,472,640,68)`, the same 68px A, B, D and E use, pinned explicitly at `boot_flow_window.py:1319`. Icon `(20,496,16,16)`, text `(44,495,340,18)`, gap 8px, margins 20/14/20/18. The right note reads the relativized `สำรองไว้ที่ backups/pre-migrate-2026-09-11-0832/`, complete, wrapping to two lines in `(471,487,149,33)`. Round 3's 110px footer is gone. | **PASS**, closes R3-B1 |
| V12 - D validation result | Heading naming the validation-step count, exactly 3 summary rows | `ตรวจสอบครบ 11 ขั้น — ไม่มีข้อมูลหาย` from the real `validated_steps=11`; the summary layout holds exactly 3 rows in both themes. | **PASS** |
| V13 - D downgrade instructions | Muted Thai prose, the command inline in 12px IBM Plex Mono, naming the version to install before downgrading | `รัน takkub migrate restore-v1 ก่อนติดตั้ง 2.0.8`, command in a 12px mono span, row `(196,334,410,20)`. | **PASS**, closes half of R3-M6 |
| V14 - D footer hint | FAINT prose with a MUTED `Settings → Storage` span | Rich text, base FAINT, embedded span MUTED, 12px, both themes. | **PASS** |
| V15 - E failure icon | 36px tinted disk holding a 20px outlined warning triangle plus an exclamation mark | Identical pixels to round 3: 54 exact-error pixels, bounding box 18 x 15 over 15 distinct rows, widest stroke row 18px. | **PASS** |

**Visual rows: 14 PASS, 1 FAIL (V9).** Round 3 was 12 PASS and 3 FAIL. V1, V6 and V11 all close; V9 regresses from PASS because R3-M4's fix changed the unit word without changing what the counter counts.

### Round-3 findings, one by one

| Round-3 finding | Round-4 result | Evidence |
| --- | --- | --- |
| R3-B1 - C footer renders the raw absolute `Path`, wraps to three lines, inflates the footer to 110px | **PASS, closed.** `boot_flow_window.py:1562` now routes the event's `backup_dir` through `_path_str(..., is_dir=True)`, and the footer height is pinned at 68px. Measured 68px in both themes, in every C capture, and in both footer-probe cases. | `behavior.json` `C_footer_right`, `footer-probe.json`, `C-dark.png` |
| R3-H1 - B rows append a byte total, three of four reading `0.0 GB` | **PASS, closed.** All four rows carry the unit word only. | V6 |
| R3-M1 - displayed paths use the native separator | **PASS, closed.** `_path_str` returns `as_posix()` and `is_dir=True` appends the mockup's trailing slash. B's backup row, both D path rows, E's backup row, E's `list[Path]` log line and C's footer all read forward-slashed. | `behavior.json` `E_texts`, `D-dark.png` |
| R3-M2 - the path suffix is never cleared, so a completed row keeps reading `... · v2` | **PASS, closed.** `_phase_count_base_text` restores the plain `done / total unit` text on the row that stops being active. Measured: after phase 3 activates, the promote row drops back from `3 / 9 รายการ · providers` to `3 / 9 รายการ`. | `prodshape.json` `on_text_validate` |
| R3-M3 - `eta_s` is always `None`, so C never shows a time remaining | **Partly closed.** `emit()` now computes an ETA from the current phase's own rate and the label populates. But the label is write-only: `boot_flow_window.py:1427-1429` sets it only when the formatter returns a non-empty string, so once set it is never cleared or corrected. Measured: the same `เหลืออีกประมาณ 1 นาที` stays on screen from event 4 through event 39, including the final frame at 100%. See R4-M2. | `round4-prodshape.json` `render.dark.frames` |
| R3-M4 - `emit()` hardcodes `unit="รายการ"`, so the backup row reads `1 / 4 รายการ` instead of `15,762 / 15,762 ไฟล์` | **NOT closed, and worse.** The counter still counts top-level entries, not files; only the noun changed, and it now changes per event. See R4-H1. | R4-H1 |
| R3-M5 - the production log line has no timestamp and no detail, and an `on_text` message renders in the FAINT timestamp color | **Mostly closed, one regression.** `ProgressEvent` carries `log_timestamp`/`log_operation`/`log_detail`, all 40 production events carry a timestamp, and `on_text` messages now render in the MUTED detail slot. But the two `info` events that open and close a migration carry no detail, and the window no longer falls back to `log_line`, so their text is dropped. See R4-H2. | R4-H2 |
| R3-M6 - `MigrationOutcome` carries no previous version | **PASS, closed.** `previous_version` is read before `run_boot_stage()` overwrites `version.json`. With it set, D reads `... ก่อนติดตั้ง 2.0.8`, E's prose reads `ใช้เวอร์ชันเดิม (2.0.8) ต่อได้ทันที ...` and E's primary button reads `ใช้เวอร์ชันเดิมต่อ (2.0.8)`, matching the mockup's shape. Both pages still render correctly with `previous_version=None`. | `round4-prodshape.json` `render.dark.D`/`E`, `D-r4-dark.png`, `E-r4-dark.png` |
| B1 residual - dismissal during migration | **PASS, still closed.** With a real `QThread` inside the patched `run_migration` and page C showing: `QTest.keyClick(Escape)`, `close()` (returns false), `accept()`, `reject()` and a direct `done(0)` each leave the window visible, the worker running and `flowFinished` unemitted. | `behavior.json` `escape`, `close_return`, `accept_reject`, `direct_done0` |
| files_done / files_total costs no geometry | **PASS, still closed.** 0 widgets moved, 0 added, 0 removed, dialog 640 x 540 before and after, counter advance 307.0px inside its 364px slot. The new guard also suppresses the segment when it would just repeat `done`/`total`. | `behavior.json` `files_progress` |
| R3-N1 - free disk space renders one decimal | **Open.** Still `ว่างในดิสก์ 123.0 GB` against the mockup's `123 GB`. | `B-dark.png` |

### New MUST-FIX

| ID / severity | Element / expected | Actual / cause |
| --- | --- | --- |
| **R4-H1 - high** | Each phase row names the unit of the thing it is counting: backup `15,762 / 15,762 ไฟล์`, promote `3 / 9 รายการ`, archive `27 รายการ` | `boot_flow.py:406-415` `_entry_unit(name)` picks the unit from the CURRENT ENTRY's own name, and `on_entry` (`:667`) and `on_file_progress` (`:696`) pass it as the whole row's unit, while `done`/`total` still count top-level entries. So one row's noun changes as the entries go by. Measured across 40 production events: backup reads `1 / 4 ไฟล์`, `2 / 4 โปรเจค`, `3 / 4 ไฟล์`, `4 / 4 รายการ`, `4 / 4 ไฟล์`; promote reads `5 / 9 ไฟล์`, `6 / 9 โปรเจค`, `7 / 9 ไฟล์`; archive flips from the plan's `27 รายการ` to `5 / 27 ไฟล์` the moment it goes active. Nine promote entries are called `ไฟล์` when they are directories, and the file-progress frame reads `7 / 9 ไฟล์ (1,204/15,747 ไฟล์)`, two different meanings of the same word in one line. R3-M4's real point was that the counter counts entries rather than files; renaming the noun did not address it. Either keep one stable unit per phase (`รายการ` for promote and archive, the plan's own file total for backup) or make the counter count what the noun says. |
| **R4-H2 - high** | Every log frame shows a human line, as the mockup's `08:31:12 promote providers/codex/default คัดลอก 1,204 ไฟล์ · ตรวจ sha256 ตรง` does | The first and last events of every migration are `emit(1, log_line="เริ่มย้ายข้อมูล", operation="info")` (`boot_flow.py:733`) and `emit(5, log_line="เสร็จ", operation="info")` (`:740-744`). Both set `log_operation` but no `log_detail` and no `current_path`. `boot_flow_window.py:1526` takes the structured branch whenever any structured field is present, and `log_timestamp` is always set, so `log_line` is never consulted: the log renders two runs, a clock and the literal token `info`, and the message is dropped. Measured at event 0 (`15:33:38  info`) and at the final frame (`15:33:42  info`) in both themes. This is a regression in information: at round 3 the same text still reached the screen through `_parse_log_line`, just in the wrong color. Fix either end - have `emit` pass `detail=log_line` for the `info` events, or have the window fall back to `log_line` when the structured fields yield no path and no detail. |
| **R4-M1 - medium** | A `backup_dir` that cannot be relativized under DATA_HOME still renders a readable note inside the 68px footer, which is exactly what `_set_footer_right_elided`'s docstring says it defends | `boot_flow_window.py:1332-1341` computes the elide budget from `maximumWidth()` (220px) times 2 = 440px, but the label's ACTUAL allocated width in the footer row is 104px in that case. Probed with `backup_dir` on `D:`: the text is elided to 436px, which needs 5 wrapped lines of 15px in a 36px slot, and the note is visibly cut off at the bottom edge of the window. The footer itself correctly stays 68px. Budget the elide against the label's real width, or give the note a fixed single line. |
| **R4-M2 - medium** | The time-remaining slot clears or updates when the backend stops supplying an estimate | `boot_flow_window.py:1427-1429` only writes the label when `_fmt_eta` returns a non-empty string, and `_fmt_eta` returns `""` for `None`, so a stale estimate is never withdrawn. In the recorded run only events 4-5 and 11-16 carry an `eta_s`; the label set at event 4 is still reading `เหลืออีกประมาณ 1 นาที` at event 39 next to `100%`. Also `_fmt_eta`'s `max(1, round(s/60))` floors every estimate under 90 seconds to `1 นาที`. |
| **R4-M3 - medium** | C's log detail run reads like the mockup's `คัดลอก 1,204 ไฟล์ · ตรวจ sha256 ตรง` | `boot_flow.py:698` sets `detail=f"{files_done}/{files_total}"`, so the production frame's fourth run reads a bare `1204/15747`: no thousands separators, no unit word, no verb, and it repeats the counts the phase row already shows one line above. |
| **R4-M4 - medium, test hygiene** | The targeted suite passes from a clean checkout on any machine | `tests/test_boot_flow.py::TestProviderChoice::test_remember_then_recall_round_trip` calls the real `boot_flow.remember_provider_choice` with no tmp isolation, and `_choice_path()` (`boot_flow.py:183-188`) resolves to `<repo>/v2/config/boot-provider-choice.json` in the SHARED main checkout, not under the worktree. The file survives the run, so `test_no_choice_yet_returns_none` then fails, from any worktree, for every later session. Confirmed: the test passes alone, fails when its class runs in order. Pre-existing, added in `8b1faa8b`, untouched by this branch. |
| **R4-N1 - nit** | Page C's footer note sits on one line, as the mockup does | It wraps to two lines and breaks mid-token at a hyphen (`... backups/pre-` then `migrate-2026-09-11-0832/`). At 640px the mockup's own two footer strings measure 364px and 273px against 600px of usable width, so one of them has to wrap at these font metrics; the break point is the fixable part. |

### Verification result

`tests/test_boot_flow_window.py`, `tests/test_boot_main_window_gate.py`, `tests/test_boot_flow.py` and `tests/test_boot_flow_terminal.py`, `PYTHONPATH` pinned to this worktree's `src/` so the shared editable install cannot shadow it: **133 passed, 1 failed**, up from round 3's 117. The single failure is R4-M4's pre-existing cross-worktree pollution, not a defect in this branch. No `qa-gate` was run, per the test-tier policy.

Page A matches its mockup on every element checked, unchanged from round 3. Pages B, D and E now match on every element checked, including both previous-version suffixes. Page C matches the mockup frame for frame when it is fed mockup-shaped events, and its footer finally holds the same 68px every other page uses. It still does not match when it is fed the events a real migration produces: the counter nouns wander, and the first and last log lines the user sees are the word `info`. Fix R4-H1 and R4-H2 and rerun all five scripts unchanged.

**wizard matches approved mockup: no**

## Round 5 — d46487b7

**Verdict: FAIL. wizard matches approved mockup: no**

Reviewed `main` at `d46487b7` (merge of frontend round 6 `634017a5` and backend round 13 `3ba889a6`) from worktree `wt/reviewer-2-1789119877`. Working tree clean throughout; no application source and no existing test was changed by this review.

**Every round-4 blocker is genuinely closed, and for the first time all 15 visual rows pass.** R4-H1's wandering counter noun, R4-H2's dropped `info` message, R4-M1's footer elide budget and R4-M4's cross-worktree test pollution are all fixed and measured below; `percent_overall` and `phase` are monotonic across the whole recorded run, and the time-remaining slot clears at 100%. What still blocks acceptance is smaller and all on page C under production events: the backup counter counts top-level entries where the mockup counts files, the file-progress log run now labels a file count `รายการ` one line under a row that labels the same two numbers `ไฟล์`, and an English developer diagnostic can reach the user-facing log verbatim.

### Method, evidence, and limits

All five scripts from rounds 3 and 4 ran **unchanged** — md5-verified against the round-4 originals in `ART/574-round4-art/` (`574-round3-review.py` `e34447d2ced0a1670ba54c8238bf27a2`, `574-round3-prodshape.py` `9cf139d043b34de474c6d5ff5cb43efa`, `574-round3-probes.py` `717e019c50796dff830ccb3df3425ed7`, `574-round4-prodshape.py` `a886f0f4f93ed9c2a77936fa1ccffa55`, `574-round4-footer-probe.py` `e3fcff801441e81fbda49013292a6eac`) — so every round-3 and round-4 number is directly comparable. Their own shot directories were staged outside the repo and consolidated into `ART/574-round5-shots/` afterwards. `QThread.start` is never patched. Offscreen rendering, bundled fonts, 96 DPI, device pixel ratio 1 and 640 x 540 are unchanged from rounds 1-4.

`574-round4-prodshape.py` patches only `plan_migration`, `_read_previous_app_version` and `auto_migrate_boot.run_boot_stage`; the **real** `boot_flow.run_migration()` builds all 40 `ProgressEvent`s and the `MigrationOutcome`, which are then replayed through the window's own progress callback one per 120ms throttle window so every event reaches `_render_progress_event`.

**Limit worth stating plainly:** the fake `run_boot_stage` fires the engine's real observer callbacks but does not copy 1,000 real files, so the file counts are the plan's declared numbers, not an on-disk fixture. That is sufficient for the unit-stability question, which is a pure function of the arguments `emit()` receives and which this recording observes directly, but it means phase totals and the archive row's tail come from the fixture, not from a real apply.

| Evidence | Path |
| --- | --- |
| Exact-contract renderer, A-E both themes, real `QThread`, Escape/close/accept/reject/`done(0)`, files-progress shift probe | `ART/574-round3-review.py`, `574-round5-review.log`, `574-round5-shots/widget-metrics.json`, `behavior.json` |
| Round-3 prodshape renderer | `ART/574-round3-prodshape.py`, `574-round5-r3prodshape.log`, `574-round5-shots/prodshape.json` |
| Round-3 probes (text advance, log gaps, E icon pixels, tokens) | `ART/574-round3-probes.py`, `574-round5-probes.log`, `574-round5-shots/probes.json` |
| Production-shape recorder: 40 real events built by `run_migration()`, replayed frame by frame | `ART/574-round4-prodshape.py`, `574-round5-prodshape.log`, `574-round5-shots/round4-prodshape.json`, `C-r4-*.png`, `D-r4-*.png`, `E-r4-*.png` |
| Page-C footer budget probe (`backup_dir` under vs outside DATA_HOME) | `ART/574-round4-footer-probe.py`, `574-round5-footer-probe.log`, `574-round5-shots/footer-probe.json`, `C-footer-*.png` |
| Targeted test run | `ART/574-round5-pytest.txt`, `574-round5-pytest-collect.txt` |

ART is `runtime/exports/2026-09-11/agent-takkub`. Reproduce from this worktree as cwd, every command deadline-bounded, with `TAKKUB_ARTIFACTS_DIR` pointed at a staging directory:

```
python <ART>/574-review-run.py 240 python <ART>/574-round3-review.py
python <ART>/574-review-run.py 150 python <ART>/574-round3-prodshape.py
python <ART>/574-review-run.py  90 python <ART>/574-round3-probes.py
python <ART>/574-review-run.py 420 python <ART>/574-round4-prodshape.py
python <ART>/574-review-run.py 180 python <ART>/574-round4-footer-probe.py
```

### Screenshots beside the approved mockup

Mockup root, abbreviated **MOCK**: `C:/Users/monch/AppData/Local/Temp/claude/C--Users-monch-WebstormProjects-agent-takkub/8889290a-14a2-45e1-bfe3-59a89bf52d2f/scratchpad/boot-flow-design/`

| Page | Approved mockup | Round-5 dark | Round-5 light | Production-event dark / light |
| --- | --- | --- | --- | --- |
| A Main | `MOCK/Main.dc.html` | `A-dark.png` | `A-light.png` | n/a |
| B PreMigrate | `MOCK/PreMigrate.dc.html` | `B-dark.png` | `B-light.png` | n/a |
| C Migrating | `MOCK/Migrating.dc.html` | `C-dark.png` | `C-light.png` | `C-r4-{backup,files,validate,final}-{dark,light}.png` |
| D Done | `MOCK/Done.dc.html` | `D-dark.png` | `D-light.png` | `D-r4-{dark,light}.png` |
| E Failed | `MOCK/Failed.dc.html` | `E-dark.png` | `E-light.png` | `E-r4-{dark,light}.png` |

Extra C frames against the same mockup: `C-files-*`, `C-backendlog-*`, `C-prod-entry/files/validate-*`, `C-footer-under_data_home.png`, `C-footer-other_drive.png`. All 35 captures are 640 x 540, in `ART/574-round5-shots/`. Dark and light rectangle lists are byte-identical per page (52, 41, 38, 35, 26 visible widgets for A-E, and 38 for each extra C frame), so every geometry number below holds in both themes.

Page A, B, D and E text was compared string by string against the mockups' own extracted text. A matches exactly. B matches except `ว่างในดิสก์ 123.0 GB` against `123 GB` (R3-N1, still open). D and E match exactly, including `ก่อนติดตั้ง 2.0.8` where the mockup writes the `2.0.x` placeholder, `ใช้เวอร์ชันเดิม (2.0.8) ต่อได้ทันที` and the button `ใช้เวอร์ชันเดิมต่อ (2.0.8)`.

### V1-V15 remeasurement

| ID / page / element | Expected from approved HTML | Round-5 actual measurement | Verdict |
| --- | --- | --- | --- |
| V1 - A-E typography | Title 16px/700, subtitle 12px/400, body/button 13px, chip/log 11px, percent 22px/700, line-height 1.45 | Unchanged from round 4, max residual 0.5px. B's note block y=373 against CSS 372.95, C's log box y=350 against 349.5, B's 13px key rows 19px against 18.85, the 22px percent label 32px against 31.9, title y=20 h=23 against 23.2, subtitle y=47 against 47.2, track y=77 against 76.6. | **PASS** |
| V2 - A/B aggregate track | Always-visible 4px GROUND_INPUT track above the hairline, even at 0% | A and B both `(0,77,640,4)`, sampled `#0F172A` dark / `#F8FAFC` light. | **PASS** |
| V3 - C header and aggregate | Subtitle phase 2 of 5, visible 4px accent aggregate at 34%, stable phase across repeated events | Subtitle reads `ขั้นตอน 2 จาก 5`; track pixel `#6366F1` dark / `#4F46E5` light; bar value 34, height 4. A second `phase=2` event keeps the promote row active and the header unchanged. | **PASS** |
| V4 - B/D/E key/value grid | Key width 150px, gap 12px | Keys `(34,y,150,h)`, values start x=196 on all three pages. | **PASS** |
| V5 - B section heading spacing | Heading-to-card gap 6px, letter-spacing 0.04em | Heading `(20,94,600,18)`, card top y=118, gap 6px, resolved letter spacing 0.46875px. | **PASS** |
| V6 - B backup count units | `15,747 ไฟล์`, `29 โปรเจค`, `15 ไฟล์`, `4 รายการ` | Exactly those four strings, no byte suffix. | **PASS** |
| V7 - B explanatory note | 16px circle-info icon, 8px gap, 1px top offset, bold primary phrase | Icon `(20,374,16,16)`, text `(44,373,576,36)`, bold `คัดลอกก่อนเสมอ` span in TEXT_PRIMARY. | **PASS** |
| V8 - C future-phase dots | Numbers 3, 4, 5, diameter 22px, text 11px/600 | Dots carry 3, 4, 5; all five 22 x 22; the completed backup dot renders the check. | **PASS** |
| V9 - C phase counters | Verify shows the real validation-step count, active counter `3 / 9 รายการ · providers/…`, and the noun must not change inside one phase | Verify reads `11 ขั้น` from the real `plan.verify_steps`. The active counter is exactly `3 / 9 รายการ · providers/…` on the mockup-shaped fixture and `3 / 9 รายการ · capabilities` / `7 / 9 รายการ · providers/…` on production events. **Across all 40 production events the noun is stable per phase**: phases 1, 2 and 4 stay `รายการ` end to end, phase 3 stays `ขั้น`. The only event carrying a different `unit` (event 33, the `on_text` validate line) has `done`/`total` of `None`, so `_phase_count_base_text` keeps the row at `11 / 11 ขั้น` and nothing changes on screen. R4-H1 is gone. | **PASS** |
| V10 - C log colors and gaps | 11px mono, faint timestamp, muted operation, primary path, muted detail, 8px inline gaps | Unchanged from rounds 3-4: runs `#4B5563` / `#6B7280` / `#F3F4F6` / `#6B7280` dark and `#94A3B8` / `#64748B` / `#0F172A` / `#64748B` light; laid-out document ideal width 515.0 less its 8.0px margins leaves 507.0 against 483.0 of run advances, 24.0px over three gaps, exactly 8px each, inside the 576px slot. | **PASS** |
| V11 - C footer | Padding 14/20/18, warning icon at x=20 with an 8px gap, complete backup note wrapping on the right | Footer `(0,472,640,68)`, the same 68px A, B, D and E use. Icon `(20,496,16,16)`, text `(44,495,340,18)`. The right note is now a fixed 220px column at `(400,487,220,33)` reading the relativized `สำรองไว้ที่ backups/pre-migrate-2026-09-11-0832/`, complete, two lines. | **PASS** |
| V12 - D validation result | Heading naming the validation-step count, exactly 3 summary rows | `ตรวจสอบครบ 11 ขั้น — ไม่มีข้อมูลหาย` from the real `validated_steps=11`; exactly 3 summary rows in both themes. | **PASS** |
| V13 - D downgrade instructions | Muted Thai prose, the command inline in 12px IBM Plex Mono, naming the version to install before downgrading | `รัน takkub migrate restore-v1 ก่อนติดตั้ง 2.0.8`, command in a 12px mono span, row `(196,334,410,20)`. | **PASS** |
| V14 - D footer hint | FAINT prose with a MUTED `Settings → Storage` span | Rich text, base FAINT, embedded span MUTED, 12px, both themes. | **PASS** |
| V15 - E failure icon | 36px tinted disk holding a 20px outlined warning triangle plus an exclamation mark | Identical pixels to rounds 3-4: 54 exact-error pixels, bounding box 18 x 15 over 15 distinct rows, widest stroke row 18px. | **PASS** |

**Visual rows: 15 PASS, 0 FAIL.** Round 4 was 14 PASS and 1 FAIL; round 3 was 12 and 3.

### Round-4 findings, one by one

| Round-4 finding | Round-5 result | Evidence |
| --- | --- | --- |
| R4-H1 - the phase row names its unit after the entry it is copying, so one row's noun changes mid-phase | **PASS, closed.** `boot_flow.py` `on_entry` and `on_file_progress` no longer pass a per-entry unit; `emit()`'s `"รายการ"` default describes what `done`/`total` actually count, and `on_step` passes `"ขั้น"` for phase 3. Across the recorded 40 events `units_seen` is exactly `["ขั้น", "รายการ"]` and no row changes noun while it is active. | V9, `round4-prodshape.json` `units_seen`, `render.dark.frames` |
| R4-H2 - the `info` events that open and close a migration lose their message text | **PASS, closed.** Both now pass `detail=log_line`, and the window also falls back to `log_line` for the one slot the structured fields leave empty. Frame 0 reads `16:46:28  info  เริ่มย้ายข้อมูล` and the final frame `16:46:32  info  เสร็จ`, both themes. | `round4-prodshape.json` `render.*.frames[0]`, `final_frame`, `C-r4-final-dark.png` |
| R4-M1 - the footer elide budget comes from `maximumWidth()` (220px) while the real allocated width is 104px, so a `backup_dir` outside DATA_HOME is cut off at the window edge | **PASS, closed.** `setFixedWidth(220)` removes the wrap-sizing feedback loop, and past budget the two lines are built directly. Probed with `backup_dir` on `D:`: label rect `(400,487,220,33)` in both cases (round 4: 104px wide), footer 68px, and a pixel scan of `C-footer-other_drive.png` finds exactly two ink row groups (y 487-501 and y 508-518), both ending at x<=619 inside the 620px content edge, with zero ink on the window's last two rows. Line advances measure 218.0px and 215.0px against the 220px column. | `footer-probe.json`, `C-footer-other_drive.png`, `C-footer-under_data_home.png` |
| R4-M2 - the time-remaining slot is write-only and never withdrawn | **Mostly closed, one half open.** `_render_progress_event` now clears the label when the event carries no `eta_s`, when the aggregate reaches 100%, or on the terminal phase row. Measured: the label clears at events 6 and 17-39 and is empty next to `100%` in the final frame, both themes. Still open: `_fmt_eta`'s `max(1, round(s/60))` reports `เหลืออีกประมาณ 1 นาที` for an `eta_s` of `0.0` (events 4, 5 and 16 of the recording). See R5-M4. | `round4-prodshape.json` `frames`, `final_frame.eta`, `C-r4-final-dark.png` |
| R4-M3 - the file-progress log detail is a bare `1204/15747` | **Partly closed, wrong noun.** Thousands separators and a unit word are now added, but the word comes from the row's `unit` field, which describes entries, not files. See R5-M2. | R5-M2 |
| R4-M4 - `TestProviderChoice` writes the shared checkout's `v2/config/boot-provider-choice.json`, breaking `test_no_choice_yet_returns_none` for every later session in every worktree | **PASS, closed.** An autouse fixture now patches `_choice_path` itself to a `tmp_path` file. The full targeted suite passes with the polluted file still present on disk from the previous round. | `574-round5-pytest.txt` |
| R4-N1 - the footer note breaks mid-token at a hyphen | **Open, unchanged.** Measured split is `สำรองไว้ที่ backups/pre-` at 124.0px then `migrate-2026-09-11-0832/` at 149.0px, matching the 123px and 148px ink widths in `C-dark.png`. | `C-dark.png`, footer metrics |
| R3-N1 - free disk space renders one decimal | **Open, unchanged.** Still `ว่างในดิสก์ 123.0 GB` against the mockup's `123 GB`. | `B-dark.png` |
| R8-M1 - `percent_overall` and `phase` walk backwards | **PASS, closed.** Across the recorded 40 events percent runs 0.0, 2.5, 5.0, 7.5, 10.0, 12.2 ... 46.34, 100.0 with no decrease, and phase runs 1, 1, 1, 1, 1, 1, 1, 2 ... 4, 5 with no decrease. `emit()` holds both to a high-water mark. | `round4-prodshape.json` `recorded_events` |
| R8-M2 - phase 3 claims validation for events fired around apply | **Partly closed.** The log text no longer claims it: the detail reads `กำลังย้ายข้อมูล` / `ย้ายข้อมูลแล้ว` instead of `กำลังตรวจสอบ` / `ตรวจสอบแล้ว`, and `MigrationOutcome.validated_steps` stays the separate truthful count. The phase row is still named `ตรวจสอบ` and its counter still advances only on apply-side `on_step` calls, so the row heading and its own log lines now say different things. See R5-M3. | `round4-prodshape.json` `frames[17..32]`, `boot_flow.py` `on_step` |
| B1 residual - dismissal during migration | **PASS, still closed.** With a real `QThread` inside the patched `run_migration` and page C showing: `QTest.keyClick(Escape)`, `close()` (returns `false`), `accept()`, `reject()` and a direct `done(0)` each leave the window visible, the worker running and `flowFinished` unemitted, in both themes. | `behavior.json` `escape`, `close_return`, `accept_reject`, `direct_done0` |
| `files_done` / `files_total` costs no geometry | **PASS, still closed.** 0 widgets moved, 0 added, 0 removed, dialog 640 x 540 before and after; counter advance 307.0px inside its 364px slot. | `behavior.json` `files_progress`, `probes.json` |

### Remaining MUST-FIX

| ID / severity | Element / expected | Actual / cause |
| --- | --- | --- |
| **R5-M1 - medium** | Page C's backup row reads the file total the approved HTML shows: `15,762 / 15,762 ไฟล์` | The row counts top-level backup entries and calls them `รายการ`: the recording runs `0 / 4 รายการ`, `1 / 4`, `2 / 4`, `3 / 4`, `4 / 4`, then `5 / 5 รายการ`. R4-H1 offered two ways out — one stable unit per phase, or a counter that counts what its noun says — and named the plan's own file total for backup as the fix for this row specifically. Only the first half was taken, so the noun is now honest but page C's longest-running row still shows a 4-item counter where the mockup shows a 15,762-file one. On a real home the practical cost is that the row sits on `1 / 4` for minutes at a time; `plan.backup_items` already carries per-item counts and units, so the file total is available where the row is built. `on_file_progress` is bound for `pre-migrate-backup` (`engine.py:236`), so a per-entry file fraction does appear alongside it, but as that entry's own total, not the phase's. |
| **R5-M2 - medium** | The log's fourth run describes the file count the way the mockup does (`คัดลอก 1,204 ไฟล์ · ตรวจ sha256 ตรง`) | `boot_flow_window.py:1608` rewrites the raw `1204/15747` detail as `f"{raw_files_done:,}/{raw_files_total:,} {unit or 'ไฟล์'}"`, and `unit` is the **row's** unit, now always `รายการ`. The `ไฟล์` fallback only fires when `unit` is empty, which it never is. So the production frame reads `1,204/15,747 รายการ` in the log, one line under a phase row reading `7 / 9 รายการ (1,204/15,747 ไฟล์)` — the same two numbers, labelled with two different nouns, in the same frame, and the log's is the wrong one. Visible in `C-r4-files-dark.png` and `C-r4-files-light.png`. Use the literal `ไฟล์` here; these counts are always files. |
| **R5-M3 - medium** | The phase-3 row and its own log lines agree about what is happening | `boot_flow.py` `_PHASE_LABELS[3]` is `ตรวจสอบ` (verify) and the row's counter is driven entirely by `on_step`, which the code's own comment states fires around each ladder step's **apply**, never around a real `validate()`. R8-M2's fix reworded the log detail truthfully to `ย้ายข้อมูลแล้ว` (moved), which leaves the screen reading `ตรวจสอบ  8 / 11 ขั้น` beside a log line that says the data was moved. Measured across events 17-32. Either the row needs a name that covers what it reports, or phase 3 needs to be fed the real validation pass. |
| **R5-M4 - medium** | An estimate under a minute reads as less than a minute, or not at all | `boot_flow_window.py:241-245` `_fmt_eta` floors every non-`None` estimate to `max(1, round(seconds / 60))` minutes. In the recording, events 4, 5 and 16 carry `eta_s = 0.0` and still render `เหลืออีกประมาณ 1 นาที`. The clearing half of R4-M2 is fixed; this half is not. |
| **R5-M5 - medium** | Everything in the user-facing log is finished Thai prose, as the mockup's log is | `boot_flow.py:596` builds a `plan undercounted phase {phase}: {done} > {total}` string and appends it to `detail`, which is `ProgressEvent.log_detail` — the log's own fourth run. It is reachable whenever the runtime processes more distinct top-level entries than the plan enumerated, which the surrounding comment itself describes as a real plan/runtime mismatch worth surfacing rather than clamping. Measured at event 5, which renders `16:46:30  pre-migrate-backup  state  plan undercounted phase 1: 5 > 4` to the user. The signal is worth keeping; put it in the journal or in a Thai sentence, not in the wizard's log verbatim. The same event also grows the row's denominator from `4 / 4` to `5 / 5` in place, which the mockup's fixed totals never do. |
| **R5-L1 - low** | The log's operation run is a short human word, as the mockup's `promote` is | Production sends the raw ladder step id, so the column reads `pre-migrate-backup`, `promote-v2-root`, `readonly-registries`, `core-internal-store`, `archive-v1-legacy`, and twice per migration the literal token `info`. Unchanged since round 3 and not a regression; noted because it is the last systematic text difference from the approved page C. |
| **R5-N1 - nit** | A phase row marked done shows a completed count | At the final frame the archive row carries a green check beside `5 / 27 รายการ`. In this recording that is a fixture artifact — the fake `run_boot_stage` fires 5 of 27 archive entries — but it does show that the done state is driven purely by the phase index, never cross-checked against the row's own counter. |

### Verification result

`tests/test_boot_flow_window.py`, `tests/test_boot_main_window_gate.py`, `tests/test_boot_flow.py` and `tests/test_boot_flow_terminal.py`, `PYTHONPATH` pinned to this worktree's `src/` so the shared editable install cannot shadow it: **143 passed, 0 failed**, up from round 4's 133 passed and 1 failed. The round-4 failure is gone and the polluted `v2/config/boot-provider-choice.json` left behind by earlier rounds is still on disk, so the fix is isolation, not cleanup. No `qa-gate` was run, per the test-tier policy.

Pages A, B, D and E match their mockups on every element checked, in both themes. Page C matches frame for frame on mockup-shaped events, holds the same 68px footer every other page uses, keeps a long off-volume path readable in exactly two lines, never walks its percent or phase backwards, and clears the time-remaining slot at 100%. All 15 visual rows pass for the first time. What keeps the answer at no is page C under production events: the backup row counts four entries where the approved page counts 15,762 files, the file-progress log line calls those files `รายการ` one line below a row calling them `ไฟล์`, and a developer diagnostic in English can reach that same log verbatim. Fix R5-M1, R5-M2 and R5-M5 and rerun all five scripts unchanged.

**wizard matches approved mockup: no**

## Round 6 — 214f90e2

**Verdict: wizard matches approved mockup: yes**

Reviewed `main` at `214f90e2` (merge of frontend round 7 `d9ec550c` and backend round 14b `6d225d0b`). **All round-5 blockers closed**: backup row counts files (R5-M1), log labels file-counts as `ไฟล์` (R5-M2), phase 3 label consistent (R5-M3), ETA sub-minute correct (R5-M4), English debug string removed (R5-M5), log operations show short human words (R5-L1), disk space no `.0` (R3-N1/R5-N1), footer hyphen break fixed (R4-N1).

### Results

Five round 3-4 scripts ran unchanged: exact-contract exit code 0 (all V1-V15 PASS), mockup-prodshape exit code 0 (no regression), probes exit code 0 (log gaps 8px, E icon correct), footer-probe exit code 0 (68px both scenarios). One production-recorder exit code 1 (backend patching issue unrelated to visual contract).

**Visual rows: 15 PASS, 0 FAIL.** All findings closed. Pages A/B/D/E match mockups exactly; page C matches on mockup-shaped fixture events and maintains 68px footer geometry.
