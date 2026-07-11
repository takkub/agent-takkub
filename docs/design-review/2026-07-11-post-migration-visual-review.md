---
date: 2026-07-11
project: agent-takkub
reviewer: critic (solo — gemini pane ไม่ได้ถูก spawn ในรอบนี้)
shots:
  - runtime/exports/2026-07-11/agent-takkub/critic-round2/01-overview.png
  - runtime/exports/2026-07-11/agent-takkub/critic-round2/04-settings.png
  - runtime/exports/2026-07-11/agent-takkub/critic-round2/06-newrole-filled.png
  - runtime/exports/2026-07-11/agent-takkub/critic-round2/02-taskdock-expanded.png
  - runtime/exports/2026-07-11/agent-takkub/critic-round2/09-roles-scrolled.png
  - runtime/exports/2026-07-11/agent-takkub/critic-round2/15-newproject.png
  - runtime/exports/2026-07-11/agent-takkub/critic-round2/crop-statusbar.png
---

# UI review · agent-takkub · 2026-07-11 · post-migration visual round 2

## 📸 Scope
Visual verification รอบใหญ่บน **display จริง** (on-screen Win32 capture — `DwmGetWindowAttribute` EXTENDED_FRAME_BOUNDS + `CopyFromScreen`, ไม่ offscreen) หลัง cockpit restart รันโค้ดที่รวมงาน UI ทั้งวัน (role-color palette ใหม่ 8 ตัว, gold-token migration, status-bar sub-groups, per-tab dots, task-dock, sidebar pending-projects, Settings IA, New Role skill-picker, New-project 1-dialog). ตรวจตาม checklist 9 ข้อ เทียบ design system ใน `.claude/skills/cockpit-ui-style/SKILL.md`. ยืนยันสีด้วยการ **sample pixel จริง** เทียบ token ไม่ใช่กะด้วยตา + cross-check source ทุก finding ให้อ้าง file:line ได้.

> วิธี verify สี: `Bitmap.GetPixel` ที่จุด dot/chip → เทียบ hex กับ `cockpit_theme` constant.

## ✅ ของดีที่ควรเก็บไว้
- **Role palette แม่นระดับ pixel** — dot ทั้ง 8 role ใน Settings render ตรง token **เป๊ะทุกตัว** (ดูตารางข้อ 1) นี่คือมาตรฐานที่อยากให้ทุก surface เป็น
- **Settings window = reference implementation** — IA ชัด (5 sidebar section), gold accent สม่ำเสมอ, mono role labels, gold toggle/CTA ถูกหลัก design system 100%
- **New-project ยุบเหลือ 1 dialog 3 ปุ่มภาษาไทย** — ตรงเป้า ลด dialog chain ที่เคยงง
- **Goalless bucket = "✅ งานทั่วไป"** — flat แล้ว ไม่มี "(ไม่ระบุเป้าหมาย)" ซ้ำ
- **gold migration ของ hard-target ครบ** — project_tab active border, update CTA, user_actions buttons ย้ายเป็น gold แล้ว (เดิม indigo/blue) ตรวจ source ยืนยัน

---

## 📋 ผล Pass/Fail ต่อข้อ

| # | รายการ | ผล | สรุป |
|---|---|---|---|
| 1 | Role colors 8 ตัว (tabs/chips) | ✅ PASS | sample pixel ตรง token ทั้ง 8 เป๊ะ |
| 2 | Gold token migration | ✅ PASS | hard-target ครบ (มี minor nit 3 จุด) |
| 3 | Status bar sub-groups + separators | ✅ PASS | 4 กลุ่ม + separator เห็นชัด (meter terse) |
| 4 | Per-pane-tab status dots | ✅ PASS | critic tab = role dot #F0619A + status dot |
| 5 | Task dock (wrap / goalless) | ⚠️ PARTIAL | goalless flat ✓ · goal-header wrap ยาวเกิน + ellipsis ไม่นิ่ง |
| 6 | Sidebar (pending section + 🧠 tooltip) | ✅ PASS* | tooltip wired ✓ · section ซ่อนถูกต้อง (ไม่มี project ค้างจริง) |
| 7 | Settings IA (5 sections) | ✅ PASS | PIPELINE/ROLE/TOOLS/SKILL/ACCOUNT แยกชัด header ไม่ซ้ำ |
| 8 | New Role skill-picker smoke | ✅ PASS† | skill line ฝังจริง · **แต่ลบ role จาก UI ไม่ได้** |
| 9 | New-project dialog | ✅ PASS | 1 dialog 3 ปุ่มไทย + Cancel ทำงาน |

\* PASS แบบ verify-by-code (สภาพ runtime ปัจจุบันไม่มี project อื่นที่มี task ค้าง จึงไม่ได้เห็น section ตอน populated)
† PASS สำหรับ skill-embed แต่มี finding เรื่อง delete (ดูข้อ 8)

### รายละเอียดข้อ 1 — role dot เทียบ token (sampled pixel)
| Role | Sampled | Token | ตรง |
|---|---|---|---|
| Lead | `#E3B341` | `#E3B341` | ✅ |
| Frontend | `#34B7AC` | `#34B7AC` | ✅ |
| Backend | `#4E86F7` | `#4E86F7` | ✅ |
| Mobile | `#A472F0` | `#A472F0` | ✅ |
| DevOps | `#43B562` | `#43B562` | ✅ |
| QA | `#E39A3C` | `#E39A3C` | ✅ |
| Reviewer | `#F26D6D` | `#F26D6D` | ✅ |
| Critic | `#F0619A` | `#F0619A` | ✅ |

critic **tab dot** (บน pane tab จริง) sample ได้ `#F0619A` เป๊ะเช่นกัน + มี status dot (เหลือง = working) ถัดมา → ข้อ 4 ผ่าน

### รายละเอียดข้อ 8 — New Role smoke test (ทำจริง)
กรอกชื่อ `test-critic-smoke` → ติ๊ก skill `cockpit-ui-style` → กด Save & Apply → ไฟล์ `~/.takkub/agents/test-critic-smoke.md` ถูกสร้าง มีบรรทัด:
```
## Skills ที่เกี่ยวข้อง
- อ่าน skill: cockpit-ui-style — The single design system for the Takkub Cockpit PyQt6 UI — gold ก่อนเริ่มงานที่เกี่ยวข้อง
```
skill-embed **ทำงานถูกต้อง** ✅ · จากนั้นพยายามลบ role: ไม่มีปุ่มลบ/trash บน row, คลิกชื่อ role ไม่เปิด edit, right-click ไม่มี context menu → **ลบจาก UI ไม่ได้** (ยืนยัน source: `settings_window.py` ไม่มี delete-role handler — มี delete แค่ Users/env/pipeline-hop/template) · cleanup ด้วยการลบไฟล์ `.md` เองผ่าน shell แล้ว

---

## ➕ เพิ่ม
- **Delete affordance ให้ custom role** — Providers & Roles row ควรมี trash/✕ (มี pattern อยู่แล้วที่ Users "Remove selected" / template "Delete") — *impact: med*
- **Tooltip legend ให้ meter `5h.../7d...`** — "5h 4:29 10% · 7d 12:29 27%" อ่านออกแต่ต้องรู้ก่อนว่า 5h/7d = rate-limit windows; ใส่ tooltip อธิบายจะช่วย first-time — *impact: low*

## ➖ ลบ
- **จำกัดความสูง goal-header ใน task dock** — ตอนนี้ header (🎯) render goal text เต็มๆ wrap 10-15 บรรทัด/goal → 3 goal กินจอเกือบหมด งาน (task rows) ตกจอ ควร clamp ~2 บรรทัด + `...` + tooltip เต็ม (เนื้อ goal ยาวควรอยู่ใน tooltip ไม่ใช่ header) — *impact: med*
- **"Feature (UI+API)" tab ใน Settings header** — มี pill highlight คล้าย active tab ค้างบน top bar ของ Settings window โดยไม่ชัดว่าเป็นอะไร (execution-plan? template?) — ถ้าไม่ได้ทำหน้าที่ใน context ของ Settings ควรเอาออก/ย้าย — *impact: low*

## 🔧 ปรับ
- **New Role: copy ปุ่มไม่ตรง helper** — ข้อความช่วยเขียน "…ตอนกด **Create Role**" แต่ปุ่มจริงชื่อ "**Save & Apply**" → เปลี่ยน helper เป็น "Save & Apply" หรือเปลี่ยนปุ่มบนหน้า New Role เป็น "Create Role" ให้ตรงกัน — *impact: low*
- **goal-header ellipsis ให้ consistent** — goal ที่ 1 ลงท้าย `...` (ตัด) แต่ goal ที่ 2 โชว์เต็มจบด้วย `(5/5)` → ตัดสินใจให้เป็นแบบเดียว (clamp+`...` ทุกอันตามข้อ "ลบ" ด้านบน) — *impact: low*
- **tutorial_overlay Next button → gold** — ปัจจุบันใช้ `METER_CLAY` (clay) เป็น CTA + มี `border-radius:6px` literal (line 107) ที่หลุด scale · design system บอก primary CTA = gold, radius ใช้ 8/10/14 เท่านั้น → เปลี่ยน Next เป็น `gold_button` + radius `RADIUS_SM`; clay เก็บไว้ที่ title ได้ (Claude-brand) — *impact: low*
- **"Up to date" idle pill สี cyan `#00A6ED`** — ปุ่ม update ตอน idle เป็น cyan/info-blue (ไม่ใช่ token ตระกูล gold/ok-green) · CTA ตอนมี update เป็น gold แล้ว (ถูก) แต่ idle state ควร tokenize (ok-green หรือ muted) ไม่ใช่ cyan ลอย — *impact: low*

## 🚩 Heuristic violations (Nielsen)
- **#3 User control & freedom** — สร้าง custom role ได้แต่ลบไม่ได้จาก UI (ต้องไป rm ไฟล์เอง) = ทางออกไม่สมมาตรกับทางเข้า (ข้อ 8) — *impact: med*
- **#4 Consistency & standards** — helper พูด "Create Role" ปุ่มเขียน "Save & Apply"; goal-header ตัด `...` บ้างไม่ตัดบ้าง — *impact: low*
- **#8 Aesthetic & minimalist** — goal-header ยาวท่วมจอเบียดงานจริงออกจาก viewport (ข้อ "ลบ" แรก) — *impact: med*

## 🧾 Note เรื่องสี status chips (design decision — ไม่ใช่ bug)
Chip กลุ่ม toggle ตอนนี้ **tokenized แล้ว** (ดี — ไม่ใช่ raw literal): `CHIP_PLAN_MAX=#8b5cf6` (violet, Max), `STATE_WARN_ALT=#f59e0b` (amber, Auto-resume ON), `CHIP_EXEC_PARALLEL=#10b981` (emerald, Multi), Codex `#10a37f` / Gemini blue (brand). แต่ละสี = ความหมายคนละอย่าง (semantic toggle palette) ไม่ใช่ accent drift — **ยอมรับได้** ในฐานะ status readout อย่างไรก็ตามมันคือ 3-4 accent แข่งกับ gold ในแถบเดียว ถ้าอยากคุมให้ minimal ขึ้นในอนาคต พิจารณาลด chroma ของ OFF-state ให้จางลง หรือใช้ gold เป็น "active" ร่วม แล้วให้ dot สีบอก identity แทน border เต็มพิลล์ — *impact: low (ทางเลือก ไม่บังคับ)*

## 🎯 Recommended next steps (สำหรับ Lead)
1. **[med]** delegate frontend เพิ่ม delete/✕ ให้ custom role ใน `settings_window.py` Providers & Roles row (reuse pattern จาก Users "Remove selected") + confirm dialog
2. **[med]** delegate frontend clamp goal-header ใน `task_dock.py` เหลือ ~2 บรรทัด + ellipsis + full-goal tooltip (คืน vertical space ให้ task rows)
3. **[low]** frontend แก้ copy New Role: helper "Create Role" → "Save & Apply" (ให้ตรงปุ่ม) · `settings_window.py`
4. **[low]** frontend: tutorial_overlay Next → `gold_button` + radius `RADIUS_SM` (เลิก 6px literal) · `tutorial_overlay.py:107,114`
5. **[low]** frontend: tokenize "Up to date" idle pill (เลิก cyan `#00A6ED`) + ตัดสินใจเรื่อง "Feature (UI+API)" tab ใน Settings header
6. **[info]** ถ้าต้องการเห็น sidebar "โปรเจคอื่นที่มี task ค้าง" ตอน populated จริง + verify click-to-open → ต้องมี pane อีก project ที่มี working task (assign แล้วยังไม่ done) — อยู่นอก scope critic ให้ QA/Lead จัด repro ถ้าต้องการ regression shot

---
_evidence ทั้งหมด: `runtime/exports/2026-07-11/agent-takkub/critic-round2/` (01-overview, 02-taskdock-expanded, 04-settings, 05..07-newrole, 09-roles-scrolled, 15-newproject + crop-* + sampled pixel logs)_
