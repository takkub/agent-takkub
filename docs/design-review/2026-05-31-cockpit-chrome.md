---
date: 2026-05-31
project: agent-takkub
reviewer: critic (gemini pane ปิดก่อน reply เต็ม — input partial, ดู note ท้ายไฟล์)
shots:
  - runtime/exports/2026-05-31/agent-takkub/screenshots/cockpit-main.png
---

# UI review · agent-takkub cockpit chrome · 2026-05-31

## 📸 Scope
รีวิว **chrome** ของหน้าต่างคอกพิท (Qt desktop app) — ไม่รวมเนื้อใน terminal (xterm.js มาตรฐาน + เป็นแค่ transcript). State ที่ถ่าย: เปิดแค่ Lead pane + status bar (teammate panes ปิด). 3 บริเวณที่ตรวจ:
1. **Title bar** (native): `agent-takkub — dev team cockpit` + min/max/close
2. **Tab bar**: `agent-takkub · 204k/1.0M ✕` + ปุ่ม `+`
3. **Pane header**: `🟢 Lead · agent-takkub` + `204k/1.0M · 20%` + icons `↓ ▾ ✕`
4. **Status bar (ล่าง)** — แถวเดียวยาว ซ้าย→ขวา:
   `🔖 /remote-control · click to bridge` | `v0.3.9-178-ga328fc9-dirty` | `Σ 204k · max 20%` | `📁` | `🗂 Logs` | `↻ Resume` | `🖥 Shell` | `🔧 Bug Check`(แดง) | `🛡 UI Review`(ชมพู) | `⚙ End Session`(เขียว) | `Max`(ม่วง) `Codex`(เขียว) `Gemini`(ฟ้า) | `🔄` | `🔌 Providers`(น้ำเงิน) | `Claude Auth`(น้ำเงิน) | `⚠ Local edits (4)`(ส้ม)

เป้าหมายรีวิว: ลด visual noise + สร้าง hierarchy ในแถบที่อัดแน่น ก่อน pre-ship.

## ✅ ของดีที่ควรเก็บไว้
- **🟢 green status dot** หน้า pane title — สื่อ "pane active" ได้ทันที ไม่ต้องอ่าน text
- **Pane header สีทอง `Lead`** — แยก role เด่นชัด ดีต่อ multi-pane scanning
- **Token meter แสดงทั้ง % + absolute** (`204k/1.0M · 20%`) — informative ครบ (ปัญหาอยู่ที่ "ซ้ำกี่ที่" ไม่ใช่ตัว format)
- **`⚠ Local edits (4)`** — proactive signal เตือน uncommitted state ดีมาก (แค่ต้องการ affordance ชัดขึ้น — ดูหัวข้อ ➕)
- **Title bar เรียบ** ตาม OS convention — ไม่ต้องแตะ

## ➕ เพิ่ม
- **Tooltip บนทุกปุ่ม status bar** — `Bug Check` / `UI Review` / `Resume` / `Shell` สื่อหน้าที่จากชื่อไม่ครบ ผู้ใช้ต้องเดา/ลองกด. เพิ่ม `title`/tooltip on-hover 1 บรรทัด — *impact: high* (Nielsen #10 help & docs, ต้นทุนต่ำ)
- **Affordance ให้ `/remote-control · click to bridge`** — ตอนนี้ดูเหมือน label เฉยๆ ทั้งที่คลิกได้. เพิ่ม hover-underline หรือ pointer cursor + bg-tint ตอน hover ให้รู้ว่า interactive — *impact: med* (Nielsen #6 recognition)
- **Click target ที่ชัดให้ `Local edits (4)`** — คลิกแล้วควรเปิด diff/`git status`; เพิ่ม hover state + tooltip "ดูไฟล์ที่แก้ยังไม่ commit" — *impact: med*

## ➖ ลบ
- **Token meter ซ้ำ 3 ที่** — โผล่ที่ (1) tab `204k/1.0M`, (2) pane header `204k/1.0M · 20%`, (3) status bar `Σ 204k · max 20%`. ข้อมูลเดียวกันเป๊ะ ใช้พื้นที่ 3 จุด. **เก็บไว้ที่ pane header** (per-pane ถูกที่สุด) + **ลบออกจาก status bar** (status bar ควรเป็น session-global ไม่ใช่ pane budget ซ้ำ). Tab เก็บได้แบบย่อ (`20%` พอ ไม่ต้อง `204k/1.0M`) — *impact: high* (ลด noise ทันที)
- **`Σ 204k · max 20%` ในสถานะ single-pane = ซ้ำกับ pane header** — เก็บไว้เฉพาะตอน multi-pane (รวมทุก pane) ค่อยมีความหมาย; single-pane ให้ซ่อน — *impact: med*
- **emoji icons นำหน้าปุ่ม** (📁 🗂 ↻ 🖥 🔧 🛡 ⚙ 🔄) — ปนหลาย style/platform-rendering, ดู playful เกินสำหรับ dev tool. ลบ emoji → ใช้ icon set เดียว (Lucide/Feather monochrome) หรือ text-only — *impact: med* (ดู 🔧 หัวข้อล่าง)

## 🔧 ปรับ
- **สีปุ่ม status bar — เลิก rainbow** — ตอนนี้ปุ่มใช้สีพื้นเต็ม 7+ สี (แดง/ชมพู/เขียว/ม่วง/ฟ้า/น้ำเงิน/ส้ม) ทุกปุ่ม "ตะโกน" เท่ากันหมด → ตาไม่รู้จะมองอะไรก่อน (Christmas-tree effect). **Spec:**
  - ปุ่ม action ปกติ (`Logs` `Resume` `Shell` `Providers` `Claude Auth`) → neutral/ghost style: bg โปร่ง, border `#3f3f46`, text `#e4e4e7`, hover bg `#27272a`
  - ปุ่ม **destructive/สำคัญเท่านั้น** ใช้สี: `End Session` → แดง (`#dc2626`), ปุ่มอื่นถอยเป็น neutral
  - `Bug Check` / `UI Review` เป็น utility → neutral + icon, อย่าใช้แดง/ชมพู filled
  - *impact: high* (คืน hierarchy — ปุ่มอันตรายเด่น ปุ่มทั่วไปเงียบ)
- **Provider chips (`Max`/`Codex`/`Gemini`) เลิก filled-pill** — ตอนนี้เป็น pill สีสดดูเหมือนปุ่มกด แต่จริงเป็น **status/toggle indicator** → affordance หลอกตา. **Spec:** เปลี่ยนเป็น outline chip + dot สถานะ (เขียว=enabled, เทา=disabled) แทนการใช้สีพื้นแยกแบรนด์; หรือถ้าเป็น toggle จริงให้ทำเป็น switch ชัดๆ — *impact: high* (Nielsen #4 consistency, #2 match real world)
- **จัดกลุ่ม status bar ด้วย divider/spacing** — ตอนนี้ทุก element เรียงแถวเดียวไม่มี separator. แบ่ง 3 กลุ่ม logical:
  1. **Context** (ซ้าย): branch · version · token
  2. **Session actions** (กลาง): Logs / Resume / Shell / Bug Check / UI Review / End Session
  3. **Config & state** (ขวา): provider chips / Providers / Claude Auth / Local edits
  - **Spec:** ใส่ `1px` vertical divider `#27272a` คั่นกลุ่ม + เพิ่ม gap ระหว่างกลุ่มเป็น `16px` (ภายในกลุ่ม `8px`) — *impact: med* (Gestalt proximity)
- **version string ตัวจางเกิน** — `v0.3.9-178-ga328fc9-dirty` เทาจางบนพื้นดำ contrast ต่ำ น่าจะหลุด WCAG AA (4.5:1) สำหรับ text เล็ก. **Spec:** color เทา `#52525b`→`#a1a1aa` หรือย่อเหลือ `v0.3.9 ·dirty` + tooltip full hash — *impact: med* (accessibility + ลด noise)
- **token meter format** — `204k/1.0M · 20%` ซ้ำข้อมูล (20% = 204k/1M). เก็บแค่ `204k · 20%` หรือ progress bar เล็ก + `20%` — *impact: low*

## 🚩 Heuristic violations (Nielsen)
- **#8 Aesthetic & minimalist** — status bar อัดทุกอย่างแถวเดียว + สีปุ่ม 7 สี + token ซ้ำ 3 ที่ = signal-to-noise ต่ำ. แก้: ลบ token ซ้ำ + neutral-ize ปุ่ม + group ด้วย divider
- **#4 Consistency & standards** — provider chips (status) หน้าตาเหมือนปุ่ม action; emoji icons ปนกับ text หลาย style. แก้: chip→outline+dot, icon set เดียว
- **#6 Recognition over recall** — `/remote-control · click to bridge` คลิกได้แต่ไม่มี affordance; ปุ่มไม่มี tooltip ต้องจำว่าแต่ละปุ่มทำอะไร. แก้: hover state + tooltip
- **#1 Visibility of system status** — token meter ซ้ำหลายที่กลับ *ลด* ความชัด (ผู้ใช้ไม่แน่ใจว่าตัวไหน per-pane ตัวไหน global). แก้: 1 ที่ต่อ scope (pane header=per-pane, status bar=global เฉพาะ multi-pane)

## 🎯 Recommended next steps (สำหรับ Lead)
1. **[high]** delegate **frontend** — neutral-ize ปุ่ม status bar (เลิก rainbow, สงวนแดงให้ End Session) + provider chips → outline+dot. ไฟล์ status bar widget (น่าจะใน `src/agent_takkub/` ฝั่ง Qt UI — frontend หา component ที่ render status bar)
2. **[high]** delegate **frontend** — ลบ token meter ซ้ำ: เก็บ pane header, ถอด/ย่อใน status bar (single-pane) + tab
3. **[med]** delegate **frontend** — เพิ่ม tooltip ทุกปุ่ม + hover affordance ให้ `/remote-control` กับ `Local edits`
4. **[med]** add ticket — group status bar 3 โซนด้วย divider + spacing (Context | Actions | Config)
5. **[low]** consider follow-up — แทน emoji icons ด้วย icon set เดียว (Lucide) + ยก contrast version string

---

### ⚠️ Note: gemini input ไม่ครบ
Lead เปิด gemini pane ขนานตาม routing และ gemini **ส่ง review รอบแรกกลับมาแล้ว** (`[gemini → critic] Visual/UX Review of cockpit-main.png:`) แต่ **เนื้อ bullet list ถูกตัดที่ฝั่ง critic เหลือเฉพาะบรรทัดหัวข้อ** และตอนพยายามขอ resend พบว่า gemini pane ปิดไปแล้ว (`takkub list` เหลือ lead + critic). Proposal นี้จึงสร้างจากมุม critic ล้วน (inspect ภาพ + crop ขยาย status bar/header/tab). หาก Lead ต้องการ cross-check รอบสอง: re-assign gemini แล้วให้ critic merge — ประเด็นที่อยากให้ gemini ช่วยยืนยันคือ contrast ratios (version string, chips) และ provider-chip affordance.
