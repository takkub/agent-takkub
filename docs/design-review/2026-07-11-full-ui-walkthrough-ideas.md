---
date: 2026-07-11
project: agent-takkub
reviewer: critic
shots:
  - runtime/exports/2026-07-11/agent-takkub/critic/00-full-cockpit.png
  - runtime/exports/2026-07-11/agent-takkub/critic/01-statusbar.png
  - runtime/exports/2026-07-11/agent-takkub/critic/02-input-chips.png
  - runtime/exports/2026-07-11/agent-takkub/critic/03-sidebar.png
  - runtime/exports/2026-07-11/agent-takkub/critic/04-taskdock.png
  - runtime/exports/2026-07-11/agent-takkub/critic/05-tabbar.png
  - runtime/exports/2026-07-11/agent-takkub/critic/06-tokenmeter.png
  - runtime/exports/2026-07-11/agent-takkub/critic/09-smartreply-ghost.png
  - runtime/exports/2026-07-11/agent-takkub/critic/07-settings-window.png
  - runtime/exports/2026-07-11/agent-takkub/critic/08-cockpit-restored.png
  - runtime/exports/2026-07-11/agent-takkub/qa/e2e-demo/01b-new-project-dialog.png
  - runtime/exports/2026-07-11/agent-takkub/qa/e2e-demo/01c-add-project-form.png
  - runtime/exports/2026-07-11/agent-takkub/qa/e2e-demo/01f-after-select-folder.png
---

# UI walkthrough · agent-takkub cockpit · 2026-07-11

## 📸 Scope

เดินสำรวจ **ทั้งคอกพิท** บน display จริง (ไม่ offscreen) — capture หน้าต่างจริงด้วย Win32
`DwmGetWindowAttribute` + `CopyFromScreen` ขณะ Lead กำลัง debug งานจริงและมี 4 pane เปิดอยู่
(Lead / Backend / Design Critic / Backend#2). รีวิว 9 พื้นที่: global layout, status bar, sidebar,
task dock, pane tabs, token meter, input (smart-reply + pointer chips), new-project 4-dialog chain,
และ Settings/Team window. ต่อยอดจาก friction ที่ QA เจอตอน E2E
(`docs/reviews/2026-07-11-e2e-real-system-test.md` § UI/UX ideas) — 2 จุดนั้นตรวจของจริง + code path
แล้ว. **ไม่ filter เอง** ตามที่สั่ง (มี critique round ต่อ) — ลง findings ทั้งหมด พร้อม impact + file:line.

## ✅ ของดีที่ควรเก็บไว้

- **Task dock iconography ชัดเจน** — ✅ done / 🔨 in-progress / ⚠️ warning อ่านออกทันที ไม่ต้องเดา (`04`, `08`)
- **Dock progress bar ต่อ project** — state ล่าสุดโชว์ `pms 3/5`, `agent-takkub 32/37` พร้อม bar (`08`) ให้ความรู้สึก "ระบบกำลังทำจริง" แข็งแรงมาก (ตรงกับที่ QA ชม)
- **Settings/Team window consolidate สะอาด** — Providers & Roles / MCP Matrix / Plugins / Skill Catalog / New Role อยู่ที่เดียว layout สม่ำเสมอ (`07`) — reviewed หลายรอบแล้ว เห็นผล
- **Status bar มี 2-group model อยู่แล้ว** (`status_header.py:582`) — โครงถูก เจตนาถูก ปัญหาอยู่ที่ execution (ดูข้างล่าง) ไม่ใช่ concept
- **Smart-reply ghost + auto-chain proposal** — flow "หนึ่งคำสั่งเข้า" ดีจริง (native affordance) แค่ discoverability ต้องดันขึ้น

## ➕ เพิ่ม

- **Per-pane-tab status dot** — pane เป็น tab ทีละอัน (ไม่มี grid; `project_tab.py:6` ยกเลิก grid เดิมเพื่อประหยัด Chromium compositor RAM). ตอน Multi fan-out หลาย pane **ดูพร้อมกันไม่ได้ ต้องคลิกทีละ tab** และ **บอกไม่ได้ว่า pane ไหน working/done โดยไม่คลิก** (ตอนนี้มีแค่ red-dot unread บน Lead เท่านั้น). เพิ่มจุดสถานะต่อ tab: spinner=working / เขียว=done / เทา=idle — QA E2E ก็เจอปัญหา "มองไม่เห็นความคืบหน้า pane อื่น" (`05`) — *impact: high*
- **Sidebar ↔ dock project consistency** — sidebar ซ้ายโชว์เฉพาะ project ที่เปิด tab (1 อัน) แต่ dock ขวาโชว์ 3 project (pms, e2e-demo-clicker, agent-takkub) → mental model "โปรเจคของฉันมีกี่อัน" ขัดกันเอง. เพิ่ม section "โปรเจคอื่นที่มี task ค้าง (คลิกเปิด tab)" ใน sidebar หรือ sync ให้ตรง (`03` เทียบ `04`) — *impact: high*
- **Token/limit meter label** — `✦ 3:38 14% / 16:21 21%` (`06`) cryptic ไม่มี label เลย — 2 คู่เวลา/% ไม่รู้อันไหน 5h-window อันไหน weekly. เพิ่ม tooltip + micro-label ("5h 14% · reset 3:38 / wk 21%") — *impact: high*
- **Role color/icon บน pane tab** — tab ตอนนี้เป็น text ล้วน สีเดียว, "Design Critic" ยาวกว่า "Backend" width ไม่สม่ำเสมอ แยกด้วยตายาก. ใส่จุดสีตาม role (frontend/backend/critic…) ให้ scan เร็ว (`05`) — *impact: med*
- **Optional 2-pane split (focused + 1 pinned)** — ให้ดู Lead + teammate 1 ตัวพร้อมกันได้ โดยจำกัด pin แค่ 1 pane เพื่อไม่ให้ RAM พุ่ง (constraint เดิมที่ทำให้ยกเลิก grid) — *impact: med*
- **Smart-reply "Tab to accept" cue** — ghost reply (`09`) render สี/weight **เหมือน text ที่ user พิมพ์เอง** ไม่มี cue ว่าเป็นข้อเสนอ (cue เดียว = cursor block ที่ตำแหน่ง 0). first-time user แยกไม่ออก. ⚠️ caveat: ghost นี้เป็น **Claude Code native terminal rendering** (ไม่ใช่ cockpit string — grep ไม่เจอใน src) → cockpit restyle ตรงๆ ยาก. ทางแก้ realistic: cockpit overlay hint strip เหนือ input เมื่อ detect ghost-suggest state, หรือ flag upstream — *impact: high (feasibility ต้องเช็คก่อน)*
- **Sidebar เติม quick-action ตอนว่าง** — มี project เดียวพื้นที่ sidebar ว่างเกือบทั้งแถบ (`03`). ใส่ recent / + task / goal chip — *impact: low*

## ➖ ลบ

- **D4 "Configure Project Paths" เมื่อ flat repo** — dialog ที่ 4 ของ new-project chain แสดง **body ว่างเปล่า** เมื่อโฟลเดอร์ไม่มี subdir ให้ map (`01f`: โชว์แค่ instruction "Map subdirectories…" + OK/Cancel ไม่มี field ให้กรอก). ใน code `_run_map_paths_dialog` (`project_wizard.py:290`) เก็บ `inputs` เฉพาะ subdir ที่ไม่ใช่ dot-dir (บรรทัด 332-340); ถ้า `not inputs` มันตกไป fallback `{"main": root}` (บรรทัด 358) อยู่แล้ว → **auto-skip เลย**: แทรก `if not inputs: return {"main": p.resolve().as_posix()}` ที่บรรทัด ~341 ก่อน `dialog.exec()`. ลด dialog 1 ตัวสำหรับเคส flat-repo (เคสที่ north-star "low-friction" ต้องการที่สุด) — *impact: high*
- **ความซ้ำซ้อน D1 ↔ D2 ใน new-project chain** — D1 (`main_window.py:932` "New project": `📂 เลือกจากที่มีอยู่` / `✨ เพิ่มโปรเจคใหม่`) แล้ว D2 (`project_wizard.py:64` "Add project": `✨ New project (AI rules)` / `📂 Import existing`) — **ทั้งคู่ถาม new-vs-existing** แค่ framing ต่าง. แย่กว่านั้น: D1 "เลือกจากที่มีอยู่" = project ที่ลงทะเบียนแล้ว vs D2 "Import existing" = โฟลเดอร์บน disk ที่ยังไม่ลง — คำเกือบเหมือน + **สลับภาษา (D1 ไทย, D2 อังกฤษ)** → user สับสนว่าต่างกันตรงไหน (`01b`, `01c`). ยุบเป็น **1 dialog 3 ปุ่ม**: `[เปิดโปรเจคที่ตั้งไว้] [โปรเจคใหม่ (AI rules)] [Import โฟลเดอร์]` — ลดอีก 1 dialog — *impact: high*
- **Header ซ้ำ 3 ที่ใน Settings** — title bar "Takkub Cockpit — Settings" + หัวหน้า "takkub cockpit — settings" + tab "takkub COCKPIT" บอกชื่อเดียวกัน 3 ครั้ง (`07`). ตัดหัวหน้าซ้ำออก 1 — *impact: low*
- **Role dots แถวบนใน "Feature (UI+API)" tab ซ้ำ Roles list** — แถว `Frontend Backend Mobile DevOps…` ด้านบน (`07`) ซ้ำกับ Roles ที่ list ด้านล่างเต็มๆ อยู่แล้ว — *impact: low*

## 🔧 ปรับ

- **Status bar: group separator ถูกกลบ** — มี 2-group model (`status_header.py:597` แทรก `_make_status_separator()` 1 เส้น) แต่ **ทุกชิปมี outline ของตัวเอง** → เส้น separator บางๆ 1 เส้นจมหายในดง outline, 14+ ชิปยัง scan เป็น blob เดียว (`01`). เพิ่ม gap ระหว่าง 2 กลุ่มให้กว้างขึ้นชัดๆ หรือลด outline ของชิปในกลุ่มเดียวกันให้ separator เด่นขึ้น — *impact: high*
- **Status bar: Group 2 ยัดของ heterogeneous 10+ ชิ้น** — `plan(Max) · exec(Multi) · auto-resume · remote · codex · gemini · rtk · restart · pipelines · update` อยู่กลุ่มเดียว (`status_header.py:598-613`) ทั้งที่คนละหมวด. แตกเป็น sub-groups คั่นด้วย separator: `[exec: Max·Multi]` · `[providers: Codex·Gemini]` · `[session: Auto-resume·Remote]` · `[system: rtk·restart·pipelines·update]` — *impact: high*
- **Status bar: Team + Local-edits ลอยนอก group model** — 2 ชิปนี้ถูก append จากคนละไฟล์ (ไม่อยู่ใน list `status_header.py:590-613`) → ไปโผล่ขวาสุดไม่มี separator คั่น (`01`). ดึงเข้า group model เดียวกัน — *impact: med*
- **Status bar: "Max" (plan) ชน "Multi" (exec) — style เกือบเหมือน** — 2 ชิปติดกัน outline คล้ายกันแต่คนละความหมาย (token plan vs execution mode) (`01`). ใส่ icon/label แยกให้ชัด หรือย้ายไม่ให้ติดกัน — *impact: med*
- **Status bar: 🔄 update button แกว่ง** — `_btn_update` (`status_header.py:422`) บางที icon-only เปล่าๆ (ambiguous — คลิกทำอะไร?), บางที "Up to date" / "Install rtk" (มี text) → ความกว้าง status bar เต้น. ให้มี label เสมอ หรือย้ายเข้า overflow "⋯" menu — *impact: med*
- **Dock: "(ไม่ระบุเป้าหมาย)" ซ้ำ + agent-takkub มี 2 group แยก** — เมื่อไม่ตั้ง goal, dock ยังใส่ group header "(ไม่ระบุเป้าหมาย)" และ agent-takkub โชว์ 2 group นี้แยกกัน (3/8 กับ 27/27) → noise + งงว่าทำไมแยก (`04`, `08`). ยุบ task ที่ไม่มี goal เป็น flat (ไม่มี header) หรือรวมเป็นกลุ่มเดียว — *impact: med*
- **Sidebar: "33% / 81%" ไม่มี label** — เลข % ข้างชื่อ project (`03`, `08`) ไม่มี legend — token budget? plan? เพิ่ม tooltip + icon (🔥) — *impact: med*
- **Pointer chips affordance ต่ำ** — `roadmap-board · task-dock · team-roles · morning-briefing · idea-mockups` (`02`) คั่น middot monospace ไม่เหมือนคลิกได้. ⚠️ native Claude Code rendering (ไม่ใช่ cockpit) → flag เหมือน smart-reply — *impact: low*
- **ภาษา dialog ปนไทย/อังกฤษ** — D1 ไทยล้วน, D2/D4 อังกฤษล้วน (`01b`/`01c`/`01f`) → unify ให้ทั้ง chain ภาษาเดียว — *impact: low*

## 🚩 Heuristic violations (Nielsen)

- **#2 Match system ↔ real world** — token meter (`06`) และ sidebar "33%" เป็นตัวเลขดิบไม่มีคำอธิบาย, user แปลไม่ออกว่าหมายถึงอะไร → เพิ่ม label/tooltip
- **#6 Recognition over recall** — status bar 14+ ชิปต้อง "จำ" ว่าอันไหนทำอะไร (`01`); pane tab ไม่มี status ต้องคลิกเพื่อรู้ว่า pane ไหนเสร็จ (`05`) → per-tab status dot + status-bar grouping
- **#4 Consistency & standards** — D1/D2 wording ทับซ้อน + ภาษาปน (`01b`/`01c`); smart-reply ghost เหมือน typed text (`09`) → unify wording, distinct suggestion styling
- **#8 Aesthetic & minimalist** — D4 empty dialog (`01f`), "(ไม่ระบุเป้าหมาย)" ซ้ำ (`04`), Settings header ซ้ำ 3 ที่ (`07`) → ตัดของที่ไม่จำเป็น

## 🌐 Multi-provider note (#103)

**Smart-reply ghost + pointer chips เป็น Claude Code native** — pane ของ **codex / gemini-agy จะไม่มี** affordance เหล่านี้ (คนละ CLI, ไม่มี ghost-suggest แบบเดียวกัน). ถ้า cockpit จะยกระดับ discoverability ของ smart-reply ต้องระวัง: อย่าผูก UI สมมติว่าทุก pane มี ghost — เป็น claude-only behavior ที่ควร flag เข้า #103 ไม่ใช่ทำเงียบ.

## 🎯 Recommended next steps (สำหรับ Lead)

1. **[high]** delegate frontend/wizard: **auto-skip D4 empty** (`project_wizard.py:~341`) + **merge D1+D2 → 1 dialog 3 ปุ่ม** (`main_window.py:918` + `project_wizard.py:64`) — ลด new-project จาก 4 → 2 dialog สำหรับ flat repo
2. **[high]** delegate frontend: **per-pane-tab status dot** (spinner/เขียว/เทา) + role color (`project_tab.py`)
3. **[high]** delegate frontend: **token meter + sidebar % label/tooltip** (`status_header.py`)
4. **[high]** delegate frontend: **sidebar ↔ dock project consistency** (แสดง project ที่มี task ค้างแม้ยังไม่เปิด tab)
5. **[med]** delegate frontend: **status-bar sub-grouping** (`status_header.py:590-613`) + update-button label + ดึง Team/Local-edits เข้า group model
6. **[med]** decision (Lead): **smart-reply discoverability** — เลือก cockpit overlay hint vs flag upstream (Claude Code native) → ticket #103 (claude-only affordance)
7. **[low]** delegate frontend: unify dialog language, dedup Settings header, ยุบ dock "(ไม่ระบุเป้าหมาย)" noise

## 📎 Evidence

ทั้งหมดใน `runtime/exports/2026-07-11/agent-takkub/` :
- `critic/00-full-cockpit.png` — full layout (4 panes, live) · `critic/08-cockpit-restored.png` — full (dock progress bars)
- `critic/01-statusbar.png` · `critic/02-input-chips.png` · `critic/03-sidebar.png` · `critic/04-taskdock.png` · `critic/05-tabbar.png` · `critic/06-tokenmeter.png` · `critic/09-smartreply-ghost.png` · `critic/07-settings-window.png`
- `qa/e2e-demo/01b-new-project-dialog.png` (D1) · `01c-add-project-form.png` (D2) · `01f-after-select-folder.png` (D4 empty) — จาก QA E2E run
