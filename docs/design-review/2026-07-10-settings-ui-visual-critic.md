---
date: 2026-07-10
project: agent-takkub
reviewer: critic (visual — ภาพจริง QWidget.grab, ไม่ใช่ code-review)
shots:
  - runtime/exports/2026-07-10/agent-takkub/critic/settings-providers-roles.png
  - runtime/exports/2026-07-10/agent-takkub/critic/settings-pipeline-builder.png
  - runtime/exports/2026-07-10/agent-takkub/critic/settings-templates.png
  - runtime/exports/2026-07-10/agent-takkub/critic/settings-mcp-matrix.png
  - runtime/exports/2026-07-10/agent-takkub/critic/settings-plugins-matrix.png
  - runtime/exports/2026-07-10/agent-takkub/critic/settings-skill-catalog.png
  - runtime/exports/2026-07-10/agent-takkub/critic/settings-new-role.png
  - runtime/exports/2026-07-10/agent-takkub/critic/settings-templates-v2.png
  - runtime/exports/2026-07-10/agent-takkub/critic/settings-skill-catalog-v2.png
  - runtime/exports/2026-07-10/agent-takkub/critic/settings-new-role-v2.png
  - runtime/exports/2026-07-10/agent-takkub/critic/settings-new-role-v3.png
  - runtime/exports/2026-07-10/agent-takkub/critic/settings-templates-v3.png
  - runtime/exports/2026-07-10/agent-takkub/critic/evidence-spinbox-v3-empty.png
  - runtime/exports/2026-07-10/agent-takkub/critic/evidence-spinbox-filefix-works.png
  - runtime/exports/2026-07-10/agent-takkub/critic/evidence-templates-v3-fixed.png
  - runtime/exports/2026-07-10/agent-takkub/critic/settings-new-role-v4.png
  - runtime/exports/2026-07-10/agent-takkub/critic/evidence-spinbox-v4-zoom.png
---

# UI review · Settings window (gold/IBM-Plex) · agent-takkub · 2026-07-10

## 📸 Scope

รีวิว **ภาพจริง** ของ `SettingsWindow` ทั้ง 7 views (Providers & Roles / Pipeline Builder / Templates / MCP Matrix / Plugins Matrix / Skill Catalog / New Role) จาก `QWidget.grab()` บนเครื่องที่มี display จริง (ไม่ใช่ offscreen QPA — font/สีจริง ไม่มี tofu). รอบก่อน user จับได้ว่าเป็นแค่ code-review — รอบนี้ดูพิกเซลจริงที่ผู้ใช้เห็นตอนกด **👥 Team**.

**Team button (step 2):** ยืนยันจาก code path — `👥 Team` (left-click) → `user_actions._on_team_chip_clicked` → `SettingsWindow(self, project=_proj, initial_view=VIEW_PROVIDERS_ROLES)` แล้ว `dlg.exec()`. เป็น class เดียวกัน initial_view เดียวกันกับที่ผมแคป → **ภาพชุดนี้ = สิ่งที่ปุ่ม Team เปิดจริง 100%**. ไม่ได้ driving live cockpit ถ่ายเพราะ `exec()` เป็น modal blocking — จะไปแย่ง session ของ user ที่กำลังเปิดอยู่ (ไม่ปลอดภัย) — เลย verify ผ่าน code แทน ซึ่งแม่นเท่ากันในกรณีนี้.

## ✅ ของดีที่ควรเก็บไว้

- **Font ผ่านฉลุย — ไม่มี tofu.** IBM Plex Sans/Mono render ครบ ทั้ง label อังกฤษและ **ข้อความไทย** (banner, hint, sub-heading) ชัดทุกตัว. นี่คือจุดที่ code-review มองไม่เห็นและเป็นเหตุผลของ re-review — ผ่าน.
- **Gold accent identity แน่น + สม่ำเสมอ.** toggle on = gold, Save & Apply gradient+glow, soft-chip template, nav active bar — ตรง design token ทุก view.
- **Providers & Roles / MCP Matrix / Plugins Matrix / Pipeline Builder / New Role** — polished, on-brand, hierarchy อ่านง่าย, role chip สีตรง spec (critic=pink, backend=blue ฯลฯ).
- **Footer copy "↺ Revert unsaved changes"** ชัดกว่า spec เดิม ("Reset to default") — เก็บไว้ ถูกต้องตามที่มันทำจริง.
- **Save & Apply ถูก disable (muted) ตอนเปิด** — affordance ถูก ไม่มีอะไร staged ก็กดไม่ได้.

## ➕ เพิ่ม

- **BUILT-IN gold badge ใน Templates list** — spec ระบุ "list ซ้าย (BUILT-IN gold badge)" แต่ตอนนี้เป็นแค่ inline text " · BUILT-IN". หลังแก้ bug #1 ควรทำเป็น gold soft-chip เล็กตาม spec — *impact: low*
- **Empty-state ของ MCP/Plugins matrix** มี label เตรียมไว้แล้ว (ดีมาก) — ไม่ต้องเพิ่ม — *impact: —*

## ➖ ลบ

- **Templates list panel กินพื้นที่เกินความจำเป็น** — มี 3 templates แต่ list ยืดเต็มความสูง ~600px เหลือที่ว่างเยอะ. พิจารณา cap ความสูงตามจำนวน item หรือให้ detail ขยายมาชิดซ้ายแทน — *impact: low*

## 🔧 ปรับ

- **[บั๊กจริง #1] Templates list = พื้นหลังสีอ่อน (native QListWidget) — อ่านแทบไม่ออก** — ใน `settings-templates.png` list ซ้ายเป็นกล่อง**สีเทาอ่อน/ขาว** ตัวหนังสือ template (gold + เทา washed) เกือบมองไม่เห็น ตัดกับ dark theme ทั้งหน้าแบบพัง. **Root cause (ยืนยันจากโค้ด):** `cockpit_theme.build_stylesheet()` มี rule ให้ `QLineEdit/QComboBox/QSpinBox/QPlainTextEdit` แต่ **ไม่มี `QListWidget` เลย** → PyQt วาด native light background. **Fix:** เพิ่ม QSS rule:
  ```
  QListWidget { background: {GROUND_PANEL}; border: 1px solid {BORDER_MED};
                border-radius: {RADIUS_SM}px; color: {TEXT_PRIMARY}; outline: none; }
  QListWidget::item { padding: 6px 8px; }
  QListWidget::item:selected { background: {GROUND_SELECT}; color: {TEXT_PRIMARY}; }
  QListWidget::item:hover { background: {GROUND_INPUT}; }
  ```
  — *impact: high*
- **[บั๊กจริง #2] Skill Catalog role list = พื้นหลังสีอ่อนเดียวกัน** — ใน `settings-skill-catalog.png` list ซ้าย (Analyst…Security) เป็น dark-text-on-light-gray → พออ่านออก แต่เป็น panel สว่างจ้าแทรกกลาง UI มืดทั้งหมด สะดุดตา. **Root cause + fix เดียวกับ #1** (แก้ที่ QSS ทีเดียวคุ้มทั้ง 2 view) — *impact: med*
- **[บั๊กจริง #3] New Role — Grid row QSpinBox ลูกศรขึ้น/ลงมองไม่เห็น** — ใน `settings-new-role.png` ช่อง "99" มีแถบปุ่มด้านขวาเป็นบล็อกสีเทาอ่อน**ไม่มี glyph ลูกศร**. โค้ด style `QSpinBox::up-button/down-button` (background) แต่**ไม่ได้ set `::up-arrow/::down-arrow` image** → PyQt วาด default arrow ที่จมหายกับพื้น. ผู้ใช้อาจไม่รู้ว่าเป็น spinner (พิมพ์เองได้อยู่). **Fix:** ใส่ `QSpinBox::up-arrow/down-arrow` เป็น glyph สี `{TEXT_SECONDARY}` (border-triangle trick หรือ svg) — *impact: low*

## 🚩 Heuristic violations (Nielsen)

- **#4 "Consistency & standards"** — Templates + Skill Catalog list มีพื้นหลังสว่างสวนทาง dark theme ทุก view ที่เหลือ = inconsistency ที่เตะตาที่สุดในทั้ง window (บั๊ก #1/#2)
- **#8 "Aesthetic & minimalist design"** — light panel ที่โผล่กลางจอมืดดึงสายตาผิดจุด (สายตาไปที่ list ว่างๆ แทน detail)

## 🎯 Recommended next steps (สำหรับ Lead)

1. **[high]** delegate **frontend** เพิ่ม `QListWidget` QSS rule ใน `cockpit_theme.py` (`build_stylesheet`, ราวบรรทัด 404 block) — แก้ทีเดียวจบทั้ง Templates (#1) + Skill Catalog (#2). เป็น visual regression ชัดเจน ควรทำก่อน ship.
2. **[low]** delegate **frontend** ใส่ `QSpinBox::up-arrow/down-arrow` glyph (New Role #3) — cosmetic ทำพร้อมรอบเดียวกันได้
3. **[low]** follow-up: Templates list BUILT-IN gold badge + cap ความสูง list (หลังแก้ #1)

> หมายเหตุ: #1/#2/#3 เป็นบั๊กระดับ CSS/QSS ล้วน — code-review รอบก่อนมองไม่เห็นเพราะไม่ได้ render จริง. ยืนยันจากทั้งภาพ (`grab()`) และโค้ด (`cockpit_theme.py` ไม่มี `QListWidget` rule) แล้ว.

---

## 🔁 Re-review หลังแก้ (2026-07-10 · v2 · ภาพจริง grab บน display จริง)

**Scope:** loop-check ตามที่ user สั่ง — capture 3 view ที่ frontend เพิ่งแก้ (Templates / Skill Catalog / New Role) แล้ว**ดูพิกเซลจริง** เทียบกับ 3 บั๊กรอบก่อน. ไม่เชื่อแค่ test.

**Shots v2:**
- `runtime/exports/2026-07-10/agent-takkub/critic/settings-templates-v2.png`
- `runtime/exports/2026-07-10/agent-takkub/critic/settings-skill-catalog-v2.png`
- `runtime/exports/2026-07-10/agent-takkub/critic/settings-new-role-v2.png`

### ผลต่อบั๊ก

| # | บั๊ก | สถานะ v2 | หลักฐานจากภาพ |
|---|---|---|---|
| **#1** | Templates list พื้นหลังสว่าง | ✅ **แก้แล้ว** | list ซ้ายเป็น **dark bg** ทั้งก้อน (GROUND_PANEL) ตัวหนังสืออ่านชัด เข้ากับ dark theme. `QListWidget` QSS rule เข้าแล้ว. **โบนัส:** BUILT-IN เป็น **gold soft-chip** ตามที่เสนอ (เดิม inline text) + list **cap ความสูงเหลือ 3 row** ไม่ยืดเต็มจอแล้ว — เก็บทั้ง 2 follow-up จากรอบก่อนไปด้วย |
| **#2** | Skill Catalog role list พื้นหลังสว่าง | ✅ **แก้แล้ว** | role list (Analyst…Security) เป็น **dark bg** เดียวกัน consistent กับทั้ง window ไม่มี panel สว่างแทรกกลางจอมืดแล้ว |
| **#3** | New Role QSpinBox ลูกศรมองไม่เห็น | ❌ **ยังไม่แก้** | ปุ่ม up/down ของช่อง "99" ยังเป็น **บล็อกสีเทาอ่อน 2 ก้อน ไม่มี glyph ลูกศรเลย** (zoom 12x ยืนยัน — ว่างเปล่า) แถมเทาอ่อนยัง**ตัดกับ dark theme** เหมือนเดิม |

### 🔧 บั๊ก #3 — ทำไมยังไม่หาย + fix ที่ถูก

frontend ใส่ QSS `QSpinBox::up-arrow/down-arrow` แบบ **CSS border-triangle trick** (`width:0; height:0; border-bottom:5px solid …`) ที่ `cockpit_theme.py:441-459`. **ปัญหา: Qt stylesheet ไม่ render border-triangle ให้ sub-control arrow** — Qt วาด `::up-arrow`/`::down-arrow` จาก `image:` / `border-image:` **เท่านั้น** (border-triangle ใช้ได้บน browser ไม่ใช่ QSS). ผลคือ arrow หายเกลี้ยง เหลือแต่ button face สีเทาอ่อน (native fallback).
- **Fix ที่ถูก:** ใช้ SVG data-URI แทน — `QSpinBox::up-arrow { image: url("data:image/svg+xml;utf8,<svg …><polygon fill='%23a1a1aa' points='0,5 8,5 4,0'/></svg>"); width:8px; height:8px; }` (down-arrow กลับ polygon). สี fill = `TEXT_SECONDARY`. หรือทำ PNG glyph เล็ก แล้ว `image: url(...)`. — *impact: low (cosmetic — พิมพ์เลขเองได้อยู่ แต่ดู broken)*
- **เสริม:** พิจารณาลดสี button face จากเทาอ่อนเป็น `GROUND_SELECT` (dark) ให้กลืน theme — ตอนนี้บล็อกเทาอ่อนสะดุดตากว่าตัว arrow เสียอีก

### ➕ ปัญหาใหม่ที่เจอ (ไม่ได้อยู่ใน 3 บั๊กเดิม)

- **Templates list — ชื่อ template ถูกตัดกลางคำ** — "Feature (UI+API)" แสดงเป็น **"Feature (UI+AP"** (clip กลาง glyph ไม่ใช่ ellipsis) เพราะ BUILT-IN chip เป็น fixed-width กินพื้นที่ label ในกล่องแคบ. อ่านชื่อไม่ครบ. **Fix:** ให้ label priority + `text-overflow`/elide เป็น "…" หรือย่อ badge ("BUILT-IN" → ไอคอน/จุด gold) หรือขยาย list ให้กว้างขึ้น — *impact: med*
- **BUILT-IN badge หนา/ใหญ่เกินตัวชื่อ** — ใน list แคบ badge เด่นกว่าชื่อ template (สายตาไปที่ "BUILT-IN" ก่อนชื่อ). ลด padding/ขนาด font ของ chip ลงหน่อยให้ชื่อนำ — *impact: low*

### 🎯 Next steps (สำหรับ Lead)

1. **[low]** delegate **frontend** แก้ #3 ให้ถูก — เปลี่ยน border-triangle → **SVG `image:` data-URI** ที่ `cockpit_theme.py:441-459` (border-triangle ไม่ทำงานใน Qt) + ลด button face เป็น dark
2. **[med]** delegate **frontend** แก้ label truncation ใน Templates list — elide "…" + ลดขนาด BUILT-IN chip ให้ชื่อ template นำ
3. **สรุป loop:** 2/3 บั๊กหาย (list light-bg #1/#2 = ✅ ปิดจบ + เก็บ follow-up gold badge/cap height ไปด้วย) · **#3 ยังค้าง** เพราะวิธี fix ผิด (Qt ไม่รับ border-triangle) · โผล่ 1 regression (label ตัดคำ). **ยังไม่ผ่าน pre-ship** — ต้อง loop รอบ frontend อีกหนึ่งเพื่อปิด #3 + truncation

---

## 🔁 Re-review รอบ 3 (final · 2026-07-10 · ภาพจริง grab บน display จริง)

**Scope:** verify เฉพาะ 2 จุดที่ frontend แก้รอบ 2 — (1) QSpinBox arrow → SVG data-URI · (2) Templates BUILT-IN chip compact + elide ชื่อยาว — **+ ไล่สวีปทั้ง 7 view อีกรอบ** ตามที่ user สั่ง ('ไล่ดูทุกปุ่มทุกเมนู อย่าให้พลาด') เผื่อ regression จากการแก้.

**Shots v3:**
- `runtime/exports/2026-07-10/agent-takkub/critic/settings-new-role-v3.png`
- `runtime/exports/2026-07-10/agent-takkub/critic/settings-templates-v3.png`
- sweep 5 view: `sweep-{providers-roles,pipeline-builder,mcp-matrix,plugins-matrix,skill-catalog}-v3.png`
- evidence crops: `evidence-spinbox-v3-empty.png` · `evidence-spinbox-filefix-works.png` · `evidence-templates-v3-fixed.png`

### ผลต่อ 2 จุดที่แก้

| # | จุด | สถานะ v3 | หลักฐาน (วัดพิกเซลจริง) |
|---|---|---|---|
| **Templates truncation** | ชื่อ template ตัดกลางคำ (v2: "Feature (UI+AP") | ✅ **แก้แล้ว** | "Feature (UI+API)" แสดง**ครบทั้งวงเล็บปิด** · "Design Review" / "Quick fix" ครบ · BUILT-IN chip ย้ายไป **คอลัมน์ขวาแยก** (outline gold ไม่ filled) ให้ชื่อ template นำ ไม่มี clip กลาง glyph แล้ว (`evidence-templates-v3-fixed.png`) |
| **#3 QSpinBox arrow** | ลูกศร up/down มองไม่เห็น | ❌ **ยังไม่หาย** | button face เปลี่ยนเป็น**dark กลืน theme แล้ว** (✅ ส่วนนี้ดีขึ้น ไม่มีบล็อกเทาอ่อนแล้ว) **แต่ glyph ลูกศรหายเกลี้ยง** — วัด button strip (x523–539) จากภาพจริง: **max brightness = 63, pixel ที่สว่าง >100 = 0 พิกเซล** (ลูกศรสี TEXT_SECONDARY `#a1a1aa`≈161 ต้องโผล่เป็นพิกเซลสว่าง — ไม่มีเลย) → `evidence-spinbox-v3-empty.png` |

### 🔧 บั๊ก #3 — ทำไม SVG data-URI ยังไม่ render + fix ที่ **พิสูจน์แล้วว่าใช้ได้**

frontend เปลี่ยนจาก border-triangle → **SVG `data:image/svg+xml;utf8,...`** (`cockpit_theme.py:238-253, 461-476`). **ปัญหา: Qt QSS `url(data:...)` ไม่ render SVG data-URI ตัวนี้.** พิสูจน์ (ไม่ใช่เดา — รัน 3 การทดลองบน PyQt6 เครื่องนี้):
- `QImageReader.supportedImageFormats()` **มี `svg`** และ `QImage.loadFromData(raw_svg, "SVG")` โหลด**สำเร็จ** (8×5, ไม่ null) → **SVG plugin มีครบ ไม่ใช่ปัญหา plugin**
- ทดสอบ QSpinBox จริงด้วย QSS 3 แบบ วัด button strip:
  - `data:...;utf8,` (ตัวที่ frontend ใช้ตอนนี้) → **ไม่มีลูกศร** (px สว่าง 0)
  - `data:...;base64,` → **ก็ไม่มีลูกศร** (max 57, px>110 = 0) — data-URI ไม่เวิร์กทั้งคู่ใน Qt QSS
  - **`url(<ไฟล์ .svg จริง>)` → ลูกศรขึ้นครบทั้ง up+down ชัดเจน** (max 227, 84 px สว่าง) → `evidence-spinbox-filefix-works.png`
- **Fix ที่ถูก (พิสูจน์แล้ว):** เขียน SVG (หรือ PNG 8×5) ลง **ไฟล์จริง** แล้วอ้าง `image: url("<path>")` — ห้ามใช้ data-URI. แนวทาง: bundle เป็น asset ถาวร (เช่น `static/spin-up.svg` / `spin-down.svg` — ไป-กลับ 2 สี normal/disabled รวม 4 ไฟล์) แล้ว build path ด้วย `pathlib.Path(__file__).parent / "static" / ...` `.as_posix()` (cross-platform Win+mac — `as_posix()` ให้ forward-slash ที่ Qt url รับได้ทั้ง 2 OS). สร้าง SVG string ครั้งเดียวตอน import แล้ว write ถ้าไฟล์ยังไม่มี ก็ได้ แต่ **ไฟล์ต้องมีจริงตอน QSS ถูก set**. — *impact: low (cosmetic — พิมพ์เลขเองได้ แต่ยังดู broken)*

### ➕ สวีปทั้ง 7 view — regression check

ไล่ดูทุก view ที่เหลือหลังการแก้รอบนี้ (ตามที่ user สั่งไล่ให้ครบ) — **ไม่พบ regression ใหม่:**
- **Providers & Roles / MCP Matrix / Plugins Matrix / Pipeline Builder** — polished, role chip สีตรง spec, toggle gold, banner/hint อ่านชัด, column aligned — เหมือนเดิม ไม่พัง
- **Skill Catalog** — role list ยังเป็น dark bg (#2 ยัง fix อยู่), markdown detail render ครบ · *nit เล็ก (ไม่ block):* item สุดท้าย "Security" โดนขอบ panel บังครึ่งบรรทัด (list scroll ได้ ไม่ใช่บั๊ก) — ปรับ padding ล่างของ list ได้ถ้าอยากเป๊ะ — *impact: low*
- **Templates** — list cap height + dark bg ยังดีจากรอบ 2, ตอนนี้ + ชื่อไม่ตัดคำแล้ว

### 🎯 สรุป loop + next step (สำหรับ Lead)

**❌ ยังไม่พร้อม ship** — เหลือ **1 บั๊กเดียว: #3 QSpinBox arrow**
- ✅ Templates truncation — ปิดจบ
- ✅ #1/#2 list light-bg — ปิดจบ (ยืนยันไม่ regress)
- ✅ สวีป 7 view — ไม่มี regression ใหม่
- ❌ **#3 arrow** — วิธี fix ผิด**เป็นรอบที่ 2** (border-triangle → data-URI ทั้งคู่ไม่ render ใน Qt QSS)

1. **[low · แต่ block loop]** delegate **frontend** ปิด #3 ให้ถูก — เปลี่ยน `data:image/svg+xml` → **ไฟล์ .svg/.png จริง** อ้าง `url("<path>")` ที่ `cockpit_theme.py:238-253, 461-476` (พิสูจน์แล้วว่าไฟล์จริง render ได้, data-URI ทั้ง utf8/base64 ไม่ได้) · ใช้ `Path(__file__).parent/...as_posix()` ให้ cross-platform
2. **[low · optional]** Skill Catalog list เพิ่ม padding ล่าง กัน item สุดท้ายโดนขอบบัง

> **loop จะจบเมื่อ #3 arrow เห็น glyph จริงในภาพ grab** (วัด px สว่าง > 0 ใน button strip) — ตอนนี้ยัง 0 · เป็น cosmetic แต่ user สั่งให้ไล่ให้ผ่านครบ จึง **ยังไม่ประกาศ 'พร้อม ship'** จนกว่า #3 จะขึ้นลูกศรจริง

---

## 🔁 Re-review รอบ 4 (FINAL · 2026-07-10 · ภาพจริง grab บน display จริง)

**Scope:** verify จุดเดียวที่ค้าง — **#3 QSpinBox arrow** หลัง frontend แก้ตามที่ผมพิสูจน์ไว้รอบ 3 (data-URI → **ไฟล์ .svg จริง** อ้างด้วย `Path().as_posix()` cross-platform). capture New Role view ใหม่ (`settings-new-role-v4.png`) + ซูม spinbox 12× วัดพิกเซล button strip แบบเดียวกับรอบ 3.

**Shots v4:**
- `runtime/exports/2026-07-10/agent-takkub/critic/settings-new-role-v4.png`
- `runtime/exports/2026-07-10/agent-takkub/critic/evidence-spinbox-v4-zoom.png` (ซูม 12×)

### ผลต่อบั๊ก #3

| # | จุด | สถานะ v4 | หลักฐาน (วัดพิกเซลจริง) |
|---|---|---|---|
| **#3** | QSpinBox arrow up/down มองไม่เห็น | ✅ **แก้แล้ว — ปิดจบ** | button strip (x97–119 ใน crop) วัดจากภาพจริง: **max brightness = 212 · pixel สว่าง >100 = 44 px** (รอบ 3 ว่างเปล่า = max 63, 0 px). ซูม 12× เห็น **ลูกศรสามเหลี่ยม up + down ชัดทั้งคู่** สี light-gray บน button face dark ที่กลืน theme (`evidence-spinbox-v4-zoom.png`) |

**ทำไมรอบนี้ผ่าน:** frontend สร้างไฟล์จริง `static/icons/spin-{up,down}[-disabled].svg` แล้วอ้าง `image: url("<Path(__file__).parent/'static'/'icons'/...as_posix()>")` ใน QSS (`cockpit_theme.py:237-464`) — ตรงกับที่พิสูจน์รอบ 3 ว่า **เฉพาะไฟล์จริงเท่านั้นที่ Qt QSS render** (data-URI utf8/base64 ทั้งคู่ไม่เวิร์ก). `.as_posix()` ให้ forward-slash ที่ Qt url รับได้ทั้ง Win + mac. verify ผ่านพิกเซลจริง ไม่ใช่แค่ code.

### ➕ New Role view — full-frame check
ดูทั้งหน้าอีกรอบหลังแก้ (`settings-new-role-v4.png`) — **ไม่มี regression:** role chip สีตรง spec, gold accent + toggle gold, Name/Label/Grid/Accent/Instructions hierarchy อ่านชัด, Save & Apply muted (disabled ถูกต้อง ไม่มีอะไร staged), footer "↺ Revert unsaved changes" ครบ. spinbox "99" ตอนนี้เห็นลูกศรทั้ง 2 ตัวชัด.

### 🎯 สรุป loop — ✅ **พร้อม ship**

ครบทุกบั๊กที่เจอทั้ง 3 รอบ ปิดจบหมด:

| บั๊ก / ปัญหา | รอบที่เจอ | สถานะสุดท้าย |
|---|---|---|
| #1 Templates list พื้นหลังสว่าง | 1 | ✅ ปิดจบ (v2) |
| #2 Skill Catalog list พื้นหลังสว่าง | 1 | ✅ ปิดจบ (v2) |
| Templates BUILT-IN badge + label ตัดคำ | 1–2 | ✅ ปิดจบ (v3 — chip แยกคอลัมน์ + ชื่อครบ) |
| **#3 QSpinBox arrow มองไม่เห็น** | 1 | ✅ **ปิดจบ (v4 — ไฟล์ .svg จริง)** |
| สวีป 7 view regression | 3 | ✅ ไม่มี regression |

**ไม่เหลือ blocker** — Settings window (gold/IBM-Plex) **พร้อม ship**. รอบนี้เป็นรอบสุดท้ายของ loop นี้.

> nit optional ที่ไม่ block ship (เก็บเป็น follow-up ได้ ถ้าอยากเป๊ะ): Skill Catalog list เพิ่ม padding ล่างกัน item สุดท้ายโดนขอบบัง (impact: low)
