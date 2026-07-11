---
date: 2026-07-11
project: agent-takkub
reviewer: critic
view: Settings management (redesign) — full 5-menu re-review (Roles / Skills / MCP / Plugins / Providers)
flag: TAKKUB_SETTINGS_UI=new
verdict: PASS
shots:
  - runtime/exports/2026-07-11/agent-takkub/critic-redesign-r2/search-back.png
  - runtime/exports/2026-07-11/agent-takkub/critic-redesign-r2/filter-custom.png
  - runtime/exports/2026-07-11/agent-takkub/critic-redesign-r2/lead-detail.png
  - runtime/exports/2026-07-11/agent-takkub/critic-redesign-r2/lead-advanced.png
  - runtime/exports/2026-07-11/agent-takkub/critic-redesign-r2/custom-advanced.png
  - runtime/exports/2026-07-11/agent-takkub/critic-redesign-r2/delete-confirm.png
  - runtime/exports/2026-07-11/agent-takkub/critic-redesign-r2/skills-full.png
  - runtime/exports/2026-07-11/agent-takkub/critic-redesign-r2/skill-detail.png
  - runtime/exports/2026-07-11/agent-takkub/critic-redesign-r2/mcp-managed-detail.png
  - runtime/exports/2026-07-11/agent-takkub/critic-redesign-r2/plugins-full.png
  - runtime/exports/2026-07-11/agent-takkub/critic-redesign-r2/plugin-blocked-detail.png
  - runtime/exports/2026-07-11/agent-takkub/critic-redesign-r2/plugin-blocked-roles.png
  - runtime/exports/2026-07-11/agent-takkub/critic-redesign-r2/providers-full.png
  - runtime/exports/2026-07-11/agent-takkub/critic-redesign-r2/provider-claude.png
  - runtime/exports/2026-07-11/agent-takkub/critic-redesign-r2/provider-codex.png
---

# UI review · Settings redesign (new) · full 5-menu · R2 · 2026-07-11

## 📸 Scope
Re-review รอบ 2 ของ **Settings window ใหม่** (`TAKKUB_SETTINGS_UI=new python -m agent_takkub.settings_management`) หลัง frontend แก้ 3 blockers จากรอบแรก + เพิ่ม Phase 3/4 (Plugins, Providers). ทดสอบบน display จริง (Win32 capture + click/type/scroll automation) ครบทั้ง **5 เมนู** (Roles / Skills / MCP Servers / Plugins / Providers) รวมสร้าง+ลบ custom role จริง 1 ตัวเพื่อพิสูจน์ CRUD + danger-zone gating. เป้าหมายที่ตัดสิน: "คนไม่เคยใช้เปิดมาแล้วรู้เลยว่าเพิ่ม/ลบ/แก้ตรงไหน ไม่งง" + design token cockpit-ui-style (gold #E3B341, IBM Plex, dark grounds).

## ⚖️ Verdict: **PASS** — 3 blockers เดิมหายจริงทุกตัว · Plugins + Providers ผ่าน spec · CRUD pattern สม่ำเสมอข้าม 5 เมนู

รอบแรก FAIL เพราะ search/filter ไม่ทำงาน, built-in โชว์ danger zone, canvas ขาว OS. **รอบนี้ยืนยันด้วยการทดลองจริงว่าทั้ง 4 จุดถูกแก้หมด** และ 2 หน้าใหม่ (Plugins/Providers) ทำได้ตาม acceptance เป๊ะ. ที่เหลือเป็น **consistency + polish (med/low)** ไม่ใช่ blocker — ไม่กระทบเป้าหมาย "รู้เลยว่าเพิ่ม/ลบ/แก้ตรงไหน" → ship ได้, refine ต่อได้แบบ non-blocking.

### Per-check result
| # | Check | ผล |
|---|---|---|
| 1a | **Search กรองสด** (ทุกเมนู) | ✅ **PASS** — พิมพ์ "back" → list เหลือ "Backend" ตัวเดียวทันที |
| 1b | **Filter chips ทำงาน + active เห็นชัด** | ✅ **PASS** — คลิก Custom → list ว่าง (ไม่มี custom จริง) + chip active มี fill เห็นชัด (label ไม่หายแล้ว) |
| 1c | **built-in ไม่มี Danger zone** | ✅ **PASS** — Lead (General + Advanced) ไม่มี danger zone/Delete เลย · custom role มี Danger zone+Delete บน Advanced · MCP managed = ไม่มี danger zone (protected) |
| 1d | **Canvas dark theme (ไม่ใช่เทา OS)** | ✅ **PASS** — พื้น header/list/detail/footer เป็น dark ground ทั้งหมด token ตรง |
| 2 | **Plugins: Installed/Blocked · BLOCKED BY COCKPIT + reason + assignment disabled · Install/Uninstall** | ✅ **PASS** — ครบทุกข้อ (ดู §Plugins) |
| 3 | **Providers: Claude REQUIRED toggle disabled + reason · Codex/Gemini installed · read-only spec · ไม่มี New** | ✅ **PASS** — ครบทุกข้อ (ดู §Providers) |
| 4 | **Consistency ข้าม 5 เมนู** | ⚠️ **PASS-with-notes** — โครง pattern สม่ำเสมอมาก แต่มี 6 จุดแตกแถวระดับ med/low (ดู §Consistency) |
| 5 | **มุมที่ยัง 'งง'** | เหลือน้อยมาก — carryover R1 เป็นหลัก (ดู §carryover) |

---

## ✅ ของดีที่ควรเก็บไว้
- **3 blockers R1 หายจริงทุกตัว** — พิสูจน์ด้วย interaction จริง ไม่ใช่แค่ดูภาพนิ่ง *impact: high*
- **CRUD end-to-end ครบวง** — สร้าง `criticr2test` → row โผล่+selected+instructions template auto → ลบผ่าน Danger zone → dialog แสดง effect (custom-roles.json / .md / Skill policy) → ยืนยันไฟล์ `~/.takkub/agents/criticr2test.md` หายจริง + row หายจริง *impact: high*
- **Danger-zone gating ถูกต้องตาม "ลบได้ค่อยโชว์"** — built-in role / managed MCP / REQUIRED provider = ซ่อน destructive affordance; custom role = โชว์ → false affordance R1 หมดไป *impact: high*
- **Plugins BLOCKED path ทำได้สวย** — red banner "BLOCKED BY COCKPIT" + เหตุผล specific + Allowed-roles tab เขียน "(assignment disabled — blocked by cockpit)" ตรงประเด็น *impact: high*
- **Providers ป้องกัน Claude ถูกวิธี** — toggle disabled (muted) + reason "cockpit infrastructure บังคับใช้" + spec fields เป็น read-only value row (ไม่ใช่ input) + Capabilities checklist (Claude ✓✓✓✓✓ vs Codex ✗✗✗✗✗) *impact: high*
- **Ownership badge ใน detail header เป็น pill จริงแล้ว** — BUILT-IN (neutral) / CUSTOM (gold soft chip) / PROJECT / MANAGED / EXTERNAL / REQUIRED — mono pill ตาม token *impact: med*
- **CRUD grammar สม่ำเสมอข้าม 5 เมนู** — title + search + filter row + list panel + "Select an item…" empty state + bottom CTA + Save/Discard footer + gold tab underline = ท่าเดียวกันทุกหน้า → เป้าหมาย "รู้เลยว่าอยู่ตรงไหน" สำเร็จ *impact: high*
- **CTA verb ถูก semantics** — Roles/Skills/MCP = "+ New X" · Plugins = "+ Install Plugin" · Providers = ไม่มีปุ่ม (fixed set) — verb ตรงกับ mental model ของแต่ละ entity *impact: med*

---

## ➕ เพิ่ม
- **Empty-state guidance เมื่อ filter ไม่มีผล** — คลิก Custom (ไม่มี custom role) → list ว่างเปล่าเงียบ ควรมี "ยังไม่มี custom role — กด + New Role เพื่อสร้าง"; หลักการเดียวกันกับ Plugins/Blocked ที่ว่าง — *impact: med* (carryover R1)
- **Toast/inline feedback หลัง create/delete** — ตอนนี้ create/delete สำเร็จเงียบ (row เปลี่ยนเฉยๆ) ผู้ใช้ต้องเดาว่าสำเร็จไหม — *impact: med* (carryover R1)
- **Role color dot ในแต่ละ row (Roles list)** — row ยังเป็น text ล้วน "Lead · built-in" ไม่มี dot สีตาม ROLE_COLORS → role identity หายทั้งหน้า — *impact: med* (carryover R1)

## ➖ ลบ
- **Horizontal scrollbar + text clipping ใน list rows (Skills + Plugins)** — description ยาว ("cockpit-ui-style · project — The single design syste…") และ `name@marketplace` ยาว ("security-guidance@claude-plugins-official · BLO…") ล้นขวา → เกิด h-scrollbar + **BLOCKED tag ของ security-guidance ถูกตัดหาย** ใน list. ควร ellipsize row text (`text-overflow`) แทนให้ล้น — *impact: med*
- **ช่อง Key/Marketplace แบบ editable บน blocked/external plugin** — เป็น identity ไม่ควรแก้ ควรเป็น read-only value row เหมือน Version/Scope — *impact: low*
- **built-in role General fields ยัง editable QLineEdit** — Role ID / Display name / Color ของ Lead ยังพิมพ์แก้ได้ ทั้งที่ definition locked (มี Save changes ที่ทำอะไรไม่ได้จริง) ควรเป็น value row + "definition locked" — *impact: med* (carryover R1)

## 🔧 ปรับ
- **[Consistency] Danger zone อยู่คนละ tab ระหว่าง entity** — Roles = **Advanced tab** · Skills = **General tab (ท้าย)** · Plugins = **General tab (ท้าย)**. ผู้ใช้ที่เรียนรู้จาก Roles ว่า "ลบอยู่ Advanced" จะหาไม่เจอใน Skills → สวนเป้าหมาย "delete อยู่ที่เดียว". Fix: วาง Danger zone ที่ **ตำแหน่งเดียวกันทุก entity** (แนะนำ: ท้าย tab สุดท้าย หรือ zone ตายตัวใต้ footer แยกจาก tab) — *impact: med*
- **[Consistency] Field vertical rhythm ไม่เท่ากัน** — MCP General (Name→Type→Command→Args→Env) เว้นบรรทัดห่างมากเทียบกับ Roles/Skills ที่ stack ชิด → หน้าตาเหมือนคนละระบบ. ใช้ spacing constant เดียวกันทุก detail form — *impact: med*
- **[Consistency] ownership ใน list row เป็น text "· built-in" ไม่ใช่ pill** — detail header ใช้ pill แล้ว แต่ list row ยังเป็น "Lead · built-in" / "context7 · user" / "remember · BLOCKED" เป็น text จาง คั่นด้วย "·". ทำให้ pill ใน detail ไม่ต่อเนื่องกับ list. ควรใช้ mini-pill ใน row หรือ align pill treatment ทั้งสองที่ — *impact: med*
- **[Consistency] badge terminology ต่างคำ concept เดียว** — Providers list = "REQUIRED" แต่ detail pill = "BUILT-IN" (Claude ตัวเดียวกัน). เลือกคำเดียว — *impact: low*
- **[Consistency] Providers ไม่มี filter-chip row** (อีก 4 หน้ามี) — justify ได้ (3 provider ตายตัว) แต่ทำให้ list เริ่มสูงกว่าหน้าอื่น (vertical alignment เพี้ยนเวลาสลับเมนู). พิจารณาใส่ chip row เปล่า/placeholder หรือคง gap ให้ list เริ่มระดับเดียวกัน — *impact: low*
- **[Design system] QSpinBox แสดงเลขไทย** — Column/Row บน Advanced tab (ทุก role) โชว์ "๑"/"๐"/"๒" ปนเลขอารบิกทั้งแอป. Fix: `spin.setLocale(QLocale(QLocale.Language.English))` — *impact: med* (carryover R1 — ยังไม่แก้)
- **[Design system] Delete/draft-guard dialog เป็น native light QMessageBox** — พื้นขาว ปุ่ม native เทา/น้ำเงิน สวน dark/gold ทั้งแอป (เนื้อหา effect list ดีอยู่แล้ว แต่ chrome ผิด). theme QMessageBox หรือทำ custom themed dialog — *impact: med* (carryover R1 — ยังไม่แก้)
- **Active filter chip เป็น fill เทากลาง ไม่ใช่ gold** — R1 บอก "label หาย" แก้แล้ว (เห็น active ชัด) แต่ active state ควรเป็น gold ตาม token (selection/active → ACCENT_GOLD) ไม่ใช่ GROUND_SELECT เทา — *impact: low*
- **Color field เป็น hex text ดิบ ไม่มี swatch** — custom role Color = "#94a3b8" เป็นตัวอักษร ไม่มี preview สี — *impact: low*
- **Delete button ใน Danger zone เป็น neutral bordered** — "Danger zone" heading แต่ปุ่ม Delete/Uninstall เป็นสีกลาง ไม่มี destructive (red) treatment → weak signal. ใส่ error-red accent ตาม semantic token — *impact: low*

## 🚩 Heuristic violations (Nielsen)
- **#4 Consistency & standards** — danger-zone คนละ tab + field spacing ไม่เท่า + เลขไทย spinbox + native light dialog = design system ยังแตกหลายจุด (แต่เบากว่า R1 มาก) *impact: med*
- **#1 Visibility of system status** — ไม่มี toast หลัง create/delete; ผู้ใช้ไม่รู้ว่าสำเร็จ *impact: med*
- **#8 Aesthetic & minimalist** — list row text clipping + h-scrollbar (Skills/Plugins) = ดูรก/ยังไม่เสร็จ *impact: med*

## 🤔 มุมที่ยัง 'งง' (check #5)
1. **หา Delete ไม่เจอเมื่อข้าม entity** — เรียนจาก Roles ว่าอยู่ Advanced แต่ Skills อยู่ General (จุดเดียวที่ยัง "งง" เชิงโครงสร้าง)
2. **"project" skill มีปุ่ม Delete** — cockpit-ui-style เป็นไฟล์ใน repo (`.claude/skills/…/SKILL.md`) แต่ลบได้ผ่าน UI → ต่างจาก built-in role ที่ป้องกัน. ควรตัดสินว่า project/shipped skill ควรลบได้ไหม (safety model ให้ตรงกับ Roles) — *ข้อสังเกต safety, ควร verify กับเจ้าของ spec*
3. เลขไทยใน spinbox — ผู้ใช้อาจสงสัยว่าค่าคืออะไร (๑ = 1?)

---

## รายละเอียดต่อหน้า

### Roles — ✅ blockers หายหมด
search กรองสด · filter Custom → ว่าง + active ชัด · Lead (built-in) ไม่มี danger zone (ทั้ง General/Advanced) · custom `criticr2test` มี Danger zone+Delete บน Advanced · ลบสำเร็จ (ไฟล์+row+registry หาย). เหลือ: role dot, ownership pill ใน row, editable built-in fields, เลขไทย spinbox, native dialog, empty state.

### Skills — ✅ pattern ตรง
title/search/filter(All/Project/Shipped/External)/list/empty-state/"+ New Skill" ครบ · detail = PROJECT pill + General/Assigned-roles tabs + Path read-only row + Danger zone(Delete) บน **General**. ติด: row text clipping + h-scroll; danger zone อยู่คนละ tab กับ Roles; project skill ลบได้ (safety?).

### MCP Servers — ✅ pattern ตรง + safety ถูก
filter All/Managed/User · chrome-devtools (managed) = MANAGED neutral pill + General/Allowed-roles/Diagnostics tabs + **ไม่มี danger zone** (protected ✓). ติด: field spacing หลวมกว่าหน้าอื่น.

### Plugins — ✅ ผ่าน spec ครบ (check #2)
filter All/Blocked · BLOCKED tag ใน list · **remember (blocked)** = red banner "BLOCKED BY COCKPIT — …เหตุผล specific" + read-only rows (Version/Scope/Enabled/Install path/Governance) + Allowed-roles tab "(assignment disabled — blocked by cockpit)" + **Uninstall** (ไม่ใช่ Delete) + bottom CTA **"+ Install Plugin"**. ✔ ทุกข้อ. (ไม่กด Uninstall จริงตามคำสั่ง).

### Providers — ✅ ผ่าน spec ครบ (check #3)
list: Claude·REQUIRED·installed / Codex·enabled·installed / Gemini·enabled·installed · **ไม่มีปุ่ม New** ✓ · **Claude** = toggle disabled (muted) + reason "cockpit infrastructure บังคับใช้ ปิดไม่ได้" + spec read-only rows (Binary/Status/Context strategy) + Capabilities ✓×5 · **Codex** = toggle **gold enabled** (interactive) + installed path + Capabilities ✗×5 → contrast ชัด. ✔ ทุกข้อ.

---

## 🎯 Recommended next steps (สำหรับ Lead)
1. **[med · consistency] delegate frontend** — วาง Danger zone ตำแหน่งเดียวกันทุก entity (Roles=Advanced ↔ Skills/Plugins=General ต่างกันอยู่) + unify field vertical spacing (MCP หลวมกว่าเพื่อน)
2. **[med] delegate frontend** — ellipsize list row text (Skills/Plugins overflow → h-scrollbar + BLOCKED tag ถูกตัด) + ทำ ownership เป็น mini-pill ใน row ให้ตรงกับ detail
3. **[med] delegate frontend** — เก็บ carryover R1 ที่ยังค้าง: `QSpinBox.setLocale(English)` (เลขไทย) + theme delete/draft QMessageBox (ตอนนี้ native light) + empty-state ของ filter + toast หลัง create/delete + role color dot
4. **[low] delegate frontend** — active filter chip → gold · badge "REQUIRED"↔"BUILT-IN" ใช้คำเดียว · Delete button ใน Danger zone ใส่ red accent · Color swatch preview
5. **[verify] ถาม spec owner** — "project"/"shipped" skill ควรลบได้ผ่าน UI ไหม (ตอนนี้ลบไฟล์ repo ได้) — safety model ให้ตรงกับ built-in role ที่ป้องกัน
6. **สรุป:** ไม่มี blocker เหลือ → เดินหน้าปิด redesign ได้; งานข้อ 1–4 เป็น polish รอบถัดไป (non-blocking) เก็บทีเดียวได้กับ 4 entity ฟรีเพราะเป็น shell-level

> **หมายเหตุ scope:** consistency defect เกือบทั้งหมดเป็น **shell-level** (detail-form layout / danger-zone placement / entity_list row / spinbox locale / dialog theme) ไม่ผูก entity ใด — แก้ครั้งเดียวได้ผลทั้ง 5 เมนู.
