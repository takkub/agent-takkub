---
date: 2026-07-11
project: agent-takkub
reviewer: critic
view: Settings management (redesign) — Roles vertical slice (Phase 0+1)
flag: TAKKUB_SETTINGS_UI=new
verdict: FAIL
shots:
  - runtime/exports/2026-07-11/agent-takkub/critic-redesign-r1/01-roles-list.png
  - runtime/exports/2026-07-11/agent-takkub/critic-redesign-r1/02-backend-detail-general.png
  - runtime/exports/2026-07-11/agent-takkub/critic-redesign-r1/03-backend-access.png
  - runtime/exports/2026-07-11/agent-takkub/critic-redesign-r1/06-new-role-filled.png
  - runtime/exports/2026-07-11/agent-takkub/critic-redesign-r1/08-draft-guard.png
  - runtime/exports/2026-07-11/agent-takkub/critic-redesign-r1/10-delete-confirm.png
  - runtime/exports/2026-07-11/agent-takkub/critic-redesign-r1/12-builtin-advanced.png
  - runtime/exports/2026-07-11/agent-takkub/critic-redesign-r1/15-search-back.png
  - runtime/exports/2026-07-11/agent-takkub/critic-redesign-r1/17-filter-custom.png
---

# UI review · Settings redesign (new) · Roles slice · 2026-07-11

## 📸 Scope
Visual + interaction review ของ **Settings window ใหม่** (`TAKKUB_SETTINGS_UI=new python -m agent_takkub.settings_management`) รอบแรก — Phase 0+1 (shell 5-entity sidebar + Roles vertical slice). ทดสอบบน display จริง (Win32 capture + click/type automation) เทียบกับ `docs/design/2026-07-11-settings-redesign-SPEC.md` + codex mockup. เป้าหมายของ redesign คือ "user เปิดมาแล้วรู้เลยว่าเพิ่ม/ลบ/แก้ตรงไหน ไม่งง" + visual เป็นไปตาม cockpit-ui-style (gold #E3B341, IBM Plex, dark grounds). รอบนี้จับ **โครงสร้าง interaction ดีมาก แต่ยังมี defect ระดับ blocker ที่ทำให้ยัง ship ไม่ได้**

## ⚖️ Verdict: **FAIL** (functional + visual defects vs. acceptance checks)

CRUD grammar ถูกทางและ core flow ทำงาน แต่ **3 check ตกชัดเจน** (search, filter, built-in danger-zone) + theme break ใหญ่ (light canvas) ที่สวนทางกับเหตุผลทั้งหมดของการ redesign → ต้อง fix-loop ก่อนปิด Phase 1.

### Per-check result
| # | Check | ผล |
|---|---|---|
| 1 | Shell: sidebar 5 เมนู · list-detail · gold token | ⚠️ PARTIAL — layout ตรง mockup แต่ **content canvas เป็นสีขาว OS default ไม่ใช่ dark ground** |
| 2 | Roles list · search + filter All/Custom/Built-in · badge · row→detail | ❌ **FAIL — search ไม่กรอง, filter ไม่กรอง**; badge เป็น text จืด ไม่ใช่ pill; ไม่มี role color dot |
| 3 | Create: + New Role → blank detail → กรอก+skill → Create → โผล่ใน list + ไฟล์จริง | ✅ PASS — สร้าง `~/.takkub/agents/critictest.md` สำเร็จ, row โผล่ทันที |
| 4 | Draft guard: แก้ field → คลิก row อื่น → ถาม Save/Discard/Keep | ✅ PASS (ทำงาน) — แต่ dialog เป็น native light, ปุ่มที่ 3 = "Cancel" ไม่ใช่ "Keep editing" |
| 5 | Access: provider dropdown + skills multi + MCP/plugins tri-state | ⚠️ PARTIAL — provider เป็น text ไม่ใช่ dropdown; tri-state สื่อกำกวม; ไม่มี search box |
| 6 | Delete: custom → danger zone → confirm effects → ลบไฟล์+policy · built-in ไม่มี danger zone | ❌ **FAIL — built-in มี Danger zone + ปุ่ม Delete (ตายด้าน)** · custom delete เอง PASS |
| 7 | หามุมที่ยัง 'งง' | จับได้หลายจุด (ดูด้านล่าง) |

---

## ✅ ของดีที่ควรเก็บไว้
- **Interaction grammar ถูกทาง** — list ซ้าย + detail ขวา + tab General/Access/Advanced + footer Save/Discard = master-detail เดียวกันตรงตาม SPEC เป๊ะ
- **Create flow ครบวง** — blank detail ใน shell เดิม (ไม่ใช่ dialog), Role ID focus + gold ring, Create ปุ่ม gold ที่ enable เฉพาะเมื่อ valid, footer เปลี่ยนเป็น Cancel/Create, สร้างไฟล์จริง + inject instruction template + selection ค้างที่ row ใหม่ *impact: high*
- **Draft guard ทำงานจริง** — แก้ field แล้วสลับ row เด้ง dialog Save/Discard/Cancel ก่อนทิ้ง draft *impact: high*
- **Delete confirm แสดง effect จริง** — "ลบ custom-roles.json / ลบไฟล์ .md / ลบ Skill policy entry" ไม่ใช่ generic "Are you sure?" + ลบไฟล์จริงหลัง confirm *impact: high*
- **CUSTOM badge เป็น soft-gold chip** (`GOLD_CHIP_*`) — เห็นชัดและถูก token
- **Forced-provider messaging** — Lead แสดง "Provider fixed by cockpit infrastructure" ใต้ช่อง provider *impact: med*
- **Gold checkbox** ของ "Use role defaults" ทำงาน (checked = gold square) + gold tab underline บน active tab

---

## ➕ เพิ่ม
- **Role color dot ในแต่ละ row** — mockup กำหนด `● Lead` (dot สีตาม `ROLE_COLORS`) แต่ตอนนี้ row เป็น text ล้วน "Lead · built-in" ไม่มี dot → role identity หายไปทั้งหน้า *impact: high*
- **Empty state ของ Custom filter** — เมื่อกรอง Custom แล้วไม่มี custom role ต้องมี "ยังไม่มี custom role — กด + New Role เพื่อสร้าง" ไม่ใช่ list เปล่า/ไม่กรอง *impact: med*
- **Toast หลัง create/delete สำเร็จ** — SPEC ระบุ `Saved <name>` / ผลลัพธ์ delete จริง; ตอนนี้เงียบ ผู้ใช้ต้องเดาว่าสำเร็จไหม *impact: med*
- **Search box ใน Access tab** (skills / MCP / plugins multi-select) — SPEC = "searchable multi-select"; ตอนนี้เป็น checkbox list ยาวไม่มีช่องค้น *impact: med*
- **Subtitle ใต้ page title** — mockup มี "Manage team identities, providers and access"; ตอนนี้มีแค่คำว่า "Roles" จาง *impact: low*

## ➖ ลบ
- **Danger zone + ปุ่ม Delete บน built-in role** — ต้อง **ซ่อนทั้งบล็อก** ไม่ใช่แค่ disable ปุ่ม (check #6 บังคับ "built-in ต้องไม่มี Danger zone"); ตอนนี้ Lead/ทุก built-in โชว์ "Danger zone" + ปุ่ม Delete ที่กดแล้วไม่เกิดอะไร = false affordance ตรงข้ามกับ iron-rule ของ codex "action ที่ทำจริงไม่ได้ต้องไม่ปรากฏเป็นปุ่ม" *impact: high*
- **ช่อง input แบบ editable บน built-in General** — Role ID / Display name / Color ของ built-in ถูก render เป็น QLineEdit แก้ได้ ทั้งที่ definition ควร locked; SPEC = แสดงเป็น value/text row + badge "definition locked" ไม่ใช่ editable input *impact: high*
- **ปุ่ม "+ New Role" ที่ลอยจาง ๆ ล่างสุดของ list** — mockup วางเป็น **gold CTA มุมขวาบนของ page header**; ตอนนี้อยู่ล่าง list เป็น text จาง มองแทบไม่เห็น = ไม่เหมือน primary action *impact: high*

## 🔧 ปรับ
- **[BLOCKER] Content canvas เป็นสีขาว OS default** — sidebar/list/detail-panel เป็น dark แต่พื้นที่ page (header, filter row, ช่องว่างระหว่าง panel, detail ว่าง, footer) เป็นเทาอ่อน #f0f0f0. **Root cause: `cockpit_theme.build_stylesheet` set background ที่ selector `QDialog#settingsWindow` เท่านั้น (บรรทัด 351) — แต่ window ใหม่เป็น `QWidget` (ไม่ใช่ QDialog) และไม่ได้ตั้ง objectName นั้น → rule ไม่ match → widget ที่ไม่มี objectName ตกไปใช้ native light.** Fix: ตั้ง `self.setObjectName(...)` + เพิ่ม QSS `QWidget#settingsManagementWindow { background: GROUND_BODY; color: TEXT_PRIMARY }` (หรือเพิ่ม base `QWidget` background rule) — จุดเดียวแก้ทั้งหน้าให้เป็น dark/gold *impact: high*
- **[BLOCKER] Search box ไม่กรอง list** — พิมพ์ "back" แล้ว list ยังโชว์ครบ 11 role. `entity_list` emit `search_changed` แต่ `management_page.refresh()` (บรรทัด 85-86) เรียก `set_items(load_rows())` แบบไม่รับ query → ต้อง wire `search_changed`/filter → re-filter rows ก่อน set_items *impact: high*
- **[BLOCKER] Filter tab All/Custom/Built-in ไม่กรอง** — คลิก Custom (ควรได้ list เปล่า) ยังโชว์ 11 built-in; และ **label ของ filter ที่ active หายไป** (มองไม่เห็นตัวอักษร) เพราะ reuse `secondaryButton` objectName ที่ไม่มี `:checked` style สำหรับ segmented control *impact: high*
- **Ownership badge ควรเป็น pill ชัด ๆ** — row ปัจจุบันแสดง "· built-in" เป็น text จืดตัวเล็ก แทนที่จะเป็น `BUILT-IN` pill (มี `source_badge.NEUTRAL_CHIP_*` อยู่แล้ว ใช้ไม่ครบใน row) *impact: med*
- **Provider ควรเป็น dropdown ไม่ใช่ text field** — Access tab แสดง provider เป็นช่องพิมพ์ "claude"; SPEC = `[Codex ▾]` single-select + สถานะ installed/enabled *impact: med*
- **Tri-state MCP/Plugins ยังกำกวม** — ปัจจุบัน = checkbox "Use role defaults" + list item checkbox ข้างล่างที่ **ยัง interactive แม้ defaults ติ๊กอยู่** → user ติ๊ก item ทั้งที่ "use defaults" on ได้ = state ขัดกัน. SPEC เตือนตรง ๆ ว่าห้ามให้ "default" กับ "no access/empty" ดูเหมือนกัน. Fix: เมื่อ "Use role defaults" on → dim/disable item list; และทำ 3-way ให้ชัด (Use defaults / Custom (N) / No access) ตามที่ data layer แยก semantics ไว้ *impact: high*
- **QSpinBox grid placement แสดงเลขเป็นเลขไทย** (Column/Row บน Advanced tab โชว์ "๖"/"๐") — QSpinBox ใช้ locale ของเครื่อง → เลขไทยปนกับ UI เลขอารบิกทั้งหน้า. Fix: `spin.setLocale(QLocale(QLocale.Language.English))` *impact: med*
- **Checkbox checked บน item list ไม่เป็น gold** — context7 (checked) แสดงเป็น ✓ ขาว/เทา ไม่ใช่ gold เหมือน "Use role defaults"; style ไม่สม่ำเสมอ *impact: low*
- **Color เป็นช่องพิมพ์ hex ดิบ (#4E86F7 / #94a3b8)** — ไม่มี swatch preview; SPEC = สีจาก `ROLE_COLORS` (custom fallback เท่านั้นเลือกเอง) *impact: low*
- **Draft-guard / delete dialog เป็น native QMessageBox สี light** — ไม่ใช่ dark/gold theme, ปุ่ม native เทา, ผิด design system. Fix: theme QMessageBox หรือทำ custom themed dialog *impact: med*
- **ปุ่มที่ 3 ของ draft guard = "Cancel"** — SPEC = "Keep editing"; "Cancel" กำกวม (ยกเลิกอะไร — การสลับ หรือการแก้?) *impact: low*
- **Detail header title ("Backend"/"New Role") contrast ต่ำ** — จางบนพื้น (ยิ่งชัดเพราะ canvas ขาว); หลังแก้ ground ต้องเช็ค `contentTitle` ว่าใช้ `TEXT_PRIMARY_ALT` *impact: low*

## 🚩 Heuristic violations (Nielsen)
- **#1 Visibility of system status** — ไม่มี toast/feedback หลัง create/delete; ผู้ใช้ไม่รู้ว่าสำเร็จ *impact: med*
- **#4 Consistency & standards** — content canvas light สวน dark theme ทั้งแอป + native dialog สี light + เลขไทยใน spinbox = design system แตกหลายจุด *impact: high*
- **#5 Error prevention** — Danger zone + Delete โผล่บน built-in (ทำไม่ได้จริง) = false affordance; input editable บน built-in ชวนให้แก้สิ่งที่ save ไม่ได้ *impact: high*
- **#6 Recognition rather than recall** — filter ที่ active label หายไป → ผู้ใช้จำไม่ได้ว่ากรองอะไรอยู่ *impact: med*
- **#8 Aesthetic & minimalist** — badge เป็น text จืด + New Role จางล่าง list + ไม่มี role dot = หน้าดูยังไม่เสร็จ *impact: med*

## 🤔 มุมที่ยัง 'งง' (check #7)
1. **Search ที่พิมพ์แล้วไม่มีอะไรเกิด** — ผู้ใช้จะคิดว่าแอปค้าง (งงกว่า UI เก่าที่อย่างน้อย matrix filter ทำงาน)
2. **Filter คลิกแล้ว label หาย + list ไม่เปลี่ยน** — ไม่รู้ว่ากรองติดไหม
3. **built-in มี Delete แต่กดไม่ได้** — "ทำไมลบ Lead ไม่ได้? ปุ่มเสียหรือ?"
4. **"Use role defaults" ติ๊กอยู่ แต่ item ข้างล่างก็ติ๊กได้** — ตกลงใช้ default หรือ custom?
5. **หน้าครึ่งขาวครึ่งดำ** — ดูเหมือน render ไม่เสร็จ/แอปพัง มากกว่าดีไซน์ตั้งใจ

## 🎯 Recommended next steps (สำหรับ Lead)
1. **[high · blocker] delegate frontend** แก้ theme root cause: window objectName + QSS base background ให้ canvas เป็น dark ground (`window.py` + `cockpit_theme.build_stylesheet:351`)
2. **[high · blocker] delegate frontend** wire search + filter → re-filter list (`widgets/management_page.py:85`, `widgets/entity_list.py`) + ทำ active-filter ให้เห็น (เลิก reuse `secondaryButton` เป็น segmented)
3. **[high] delegate frontend** ซ่อน DangerZone ทั้งบล็อกเมื่อ `plan is None`/built-in (`widgets/danger_zone.py:42` → `self.setVisible(plan is not None)`) + ทำ built-in General เป็น read-only value row (locked)
4. **[high] delegate frontend** แก้ tri-state MCP/plugins: disable item list เมื่อ "Use role defaults" on + ทำ 3-way ให้ชัด
5. **[med] delegate frontend** เพิ่ม role color dot ใน row + ทำ BUILT-IN/CUSTOM เป็น pill + ย้าย "+ New Role" เป็น gold CTA มุมขวาบน + toast หลัง create/delete
6. **[med] delegate frontend** spinbox `setLocale(English)` + provider เป็น dropdown + search box ใน Access multi-selects
7. **[low] add ticket:** theme native QMessageBox (draft-guard/delete) ให้เข้า dark/gold + copy "Cancel" → "Keep editing"
8. **หลังแก้ → re-review รอบ 2** (critic) ก่อนปิด Phase 1 / เดินหน้า Phase 2 (Skills/MCP)

> **หมายเหตุ scope:** defect ที่ blocker เป็น **shell-level (management_page / entity_list / cockpit_theme / danger_zone / window)** ไม่ใช่ roles-specific — แก้ตอนนี้จะได้ผลกับทั้ง 4 entity ที่เหลือฟรี (สำคัญมากเพราะ Phase 0+1 คือ "พิสูจน์ pattern")
