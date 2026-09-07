# Critic: design-review pipeline (screenshots → proposal.md/.html)

Read this before starting any design-critic task — it's the whole workflow.

## Input convention — screenshots from QA

QA captures screenshots into `$TAKKUB_ARTIFACTS_DIR/screenshots/` (central,
outside the repo — the cockpit sets `$TAKKUB_ARTIFACTS_DIR` to the same
path for every pane in this project):
```
$TAKKUB_ARTIFACTS_DIR/screenshots/<page-or-view>.png
```
Open and inspect them first every time: `ls -la
"$TAKKUB_ARTIFACTS_DIR/screenshots/"`. If no shots are found → `takkub send
--to lead "blocked: ไม่มี screenshots ใน \$TAKKUB_ARTIFACTS_DIR/screenshots/
— รบกวน assign QA capture ก่อน"`.

## Workflow (5 steps)

### 1. List + inspect shots
```bash
ls -la "$TAKKUB_ARTIFACTS_DIR/screenshots/"
```

**#159 — check the size before reading:** a file that's abnormally small
compared to its siblings (e.g. < 10KB while the rest are 40-100KB) is
usually a blank/failed capture from QA, not real evidence — don't count it
as "complete" just because the filename exists. If you find one, `takkub
send --to lead "blocked: screenshot <file> เล็กผิดปกติ (<size>)
น่าจะถ่ายพลาด — ขอ QA ถ่ายใหม่"` instead of continuing to review a
blank/broken file.

Read each image file with the `Read` tool — Claude sees images directly.
Look for:
- Hierarchy: is heading/body/caption clearly separated?
- Spacing: is the rhythm consistent?
- Color: does contrast pass WCAG AA?
- Affordance: do buttons look clickable? do links look like links?
- State coverage: are empty / loading / error / success states all covered?
- Mobile: does it stack/collapse/wrap well (if there's a mobile shot)?

### 2. Send the shot to Gemini via pane

⚠️ **Never spawn gemini yourself** — Lead opens a gemini pane in parallel
with you at assign time per the routing plan.

Send the path of each image to gemini via `takkub send`:
```bash
takkub send --to gemini "review UI image: $TAKKUB_ARTIFACTS_DIR/screenshots/login.png

ดูในมุม visual design + UX:
1. heuristic violations (Nielsen 10)
2. visual hierarchy issues
3. accessibility concerns
4. 3-5 actionable ideas (เพิ่ม/ลบ/ปรับ)

ตอบกลับ takkub send --to critic ด้วย bullet list"
```
Wait for gemini to reply via `takkub send --to critic` (the orchestrator
injects a CC to Lead automatically too).

### 3. Consolidate

Combine your own view (from reading the images), gemini's view (from the
`takkub send` reply), and (optional) codex's view if Lead pre-spawned it —
find the overlapping points + pick the most actionable top items.

### 4. Write the proposal markdown

```bash
mkdir -p "$TAKKUB_DOCS_DIR/design-review"
```

Create `$TAKKUB_DOCS_DIR/design-review/<YYYY-MM-DD>-<view-or-page>.md`
(central, outside the repo — the cockpit sets `$TAKKUB_DOCS_DIR` for every
pane):

```markdown
---
date: 2026-05-22
project: <project>
reviewer: critic + gemini
shots:
  - $TAKKUB_ARTIFACTS_DIR/screenshots/login.png
  - $TAKKUB_ARTIFACTS_DIR/screenshots/dashboard.png
---

# UI review · <project> · 2026-05-22

## 📸 Scope
1 paragraph: คือหน้าอะไร / flow ไหน / รีวิวเพื่ออะไร

## ✅ ของดีที่ควรเก็บไว้
- ...

## ➕ เพิ่ม
- **<idea title>** — rationale (1 ประโยค) — impact: high/med/low
- ...

## ➖ ลบ
- **<element>** — เหตุผล — impact

## 🔧 ปรับ
- **<change>** — spec ที่ frontend implement ได้เลย (เช่น "padding 16→24, color #71717a→#52525b")
- ...

## 🚩 Heuristic violations (Nielsen)
- #X "<heuristic name>" — ที่ไหน + แก้ยังไง

## 🎯 Recommended next steps (สำหรับ Lead)
1. [high] delegate frontend แก้ <X> ใน <file>
2. [med] add ticket: <Y>
3. [low] consider follow-up: <Z>
```

**Format rules (important — the converter below depends on this):**
- every finding ends its bullet with `*impact: high|med|low*` (the
  converter turns it into a colored badge + card)
- `shots:` in the front matter must list every screenshot referenced (the
  converter inlines them as base64)

### 4b. Render to HTML (self-contained — required)

After writing the `.md`, run the converter to produce a matching `.html`
(images inline as base64, impact→badge, card):
```bash
python -m agent_takkub.design_review_html "$TAKKUB_DOCS_DIR/design-review/<YYYY-MM-DD>-<view>.md"
# → OK $TAKKUB_DOCS_DIR/design-review/<YYYY-MM-DD>-<view>.html
```
The HTML is self-contained and opens straight in a browser (Lead/the user
can click the path in the pane and open it immediately) — the `.md` stays
as the source (easy to diff/grep), the `.html` is the actual review people
open.

### 5. Report back

Report **both paths** (html first — that's the one people open) — a note
with no path gets tagged `⚠ no evidence cited`:
```bash
takkub done "design review เสร็จ — \$TAKKUB_DOCS_DIR/design-review/2026-05-22-login.html (+ .md source · 3 high, 2 med, 1 low)"
```
