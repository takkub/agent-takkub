# Core V2 Phase 9 — Design Critic review (epic #309)

> reviewer: critic (solo — gemini pane not spawned for this task) · branch `feat/v2-core` @ `b32a618`
> scope: `src/agent_takkub/settings_core_v2.py` (mixin) + `core_v2_settings.py`, screenshots
> `runtime/exports/phase9/phase9-core-v2-*.png` (6), report `docs/v2/phase9-report.md`
> method: read all 6 screenshots + full source of the new mixin, diffed button/token usage
> against `settings_window.py` and the `cockpit-ui-style` skill, ran
> `tests/test_settings_core_v2.py` offscreen (21/21 pass) to confirm current state.

## ผลสรุป

**ไม่มี must-fix — ผ่าน**, มี 3 should + 3 nice ให้ frontend ทำต่อได้ถ้ามีเวลา (ไม่บล็อก merge)

Token discipline สะอาดจริง: `grep` หา hex literal (`#rrggbb`) และ hardcoded
font-family ใน `settings_core_v2.py` + `core_v2_settings.py` = **0 จุด**
ปุ่มทุกตัวมาจาก `gold_button`/`secondary_button`, panel/hint ทุกตัวใช้
`objectName` ที่ `build_stylesheet` รู้จักอยู่แล้ว (`panel`/`panelHint`/
`infoBanner`/`providerRow`), delete confirm ใช้ `themed_message_box` (ไม่ใช่ raw
`QMessageBox`) — ไม่มี native light popup โผล่แบบที่เจอในหน้าเก่าบางหน้า และ
"ไม่มีปุ่ม migration apply ใน UI" ตรวจแล้วจริงในโค้ด ไม่ใช่แค่คำอ้างใน report.

## ✅ ของดีที่ควรเก็บไว้

- **0 hardcoded hex/font ใน 2 ไฟล์ใหม่** — ผ่านทุก litmus test ของ `cockpit-ui-style`
- **Reuse `_build_card_header` helper เดิม** แทนที่จะสร้าง header pattern ใหม่ — ทำให้
  chip "1/5 on" / "STORAGE" ทุกอันหน้าตาตรงกับหน้าอื่นๆ โดยอัตโนมัติ
- **Delete confirm ใช้ `themed_message_box`** (`_on_cv2_remove_account_clicked`
  บรรทัด 622) ไม่ใช่ raw `QMessageBox` — สอดคล้องกับ pattern ที่ถูกของหน้าอื่น
- **Empty-state text มีจริงเกือบทุกจุด**: version.json ว่าง, Migration report
  ก่อนกด Refresh, Scheduler dict hint, Brain counts ก่อนกด Reindex — ข้อความ
  ภาษาไทยชัดเจนทุกจุด (ยกเว้น 1 จุด ดู should #3)
- **Disabled toggle (`TAKKUB_V2_CONTEXT`) มี tooltip อธิบายเหตุผล** ไม่ใช่แค่ปิดเฉยๆ
  ("ยังไม่มี core module ให้ toggle นี้เชื่อมถึง")
- 21/21 tests ผ่านจริงตอน re-run offscreen (ไม่ใช่แค่เชื่อ report)

## 🔧 Should-fix (ไม่บล็อก merge แต่ทำก่อน user เจอเองจะดีกว่า)

### 1. Footer "Save & Apply" ไม่เกี่ยวข้องกับ Core V2 เลย แต่ไม่มีสัญญาณบอกผู้ใช้
ทั้ง 6 หน้า Core V2 บันทึกผ่านปุ่มของตัวเอง (Overview="Save flags",
Scheduler="Save policy", Accounts=ทันทีตอน Edit/Remove) — ตรวจโค้ดยืนยันว่า
`ToggleSwitch`/policy field ใน Core V2 **ไม่ส่ง signal เข้า dirty-tracking ของ
footer เลย** (`_on_cv2_save_flags_clicked` เขียนตรงไปที่ `core_v2_settings.save()`
ไม่แตะ transaction ของ footer) แปลว่าปุ่มโกลด์ใหญ่ "Save & Apply" มุมล่างขวา
กับปุ่ม "Revert unsaved changes" **นิ่งเฉยไม่ทำอะไรกับ Core V2 เสมอ** ไม่ว่าจะ
toggle/แก้ policy ไปกี่ครั้ง — แต่ทั้งสองปุ่มมีสไตล์เดียวกันกับปุ่ม Save จริงของ
Core V2 ทุกประการ (gold, ตำแหน่งเด่น) ผู้ใช้ที่คุ้นกับหน้า Settings เดิม (ที่
"Save & Apply" คือจุดเดียวที่บันทึกทุกอย่าง) มีโอกาสสูงที่จะกด Save & Apply
คาดว่า flag ที่เพิ่ง toggle จะถูกบันทึกด้วย — แล้วมันไม่ถูก, หรือกด "Revert
unsaved changes" หลังกด "Save flags" ไปแล้ว คาดว่าจะ undo ค่าที่เพิ่งบันทึก —
แต่มันเขียนลงดิสก์ไปแล้วจริง ไม่มีอะไรให้ revert

**Impact: med-high** (ความเชื่อผิดเรื่อง state ที่บันทึกแล้วหรือยัง)
**แก้:** เพิ่ม `panelHint` บรรทัดเดียวใต้ปุ่ม Save ของแต่ละหน้า Core V2 เช่น
"บันทึกทันทีที่นี่ — แยกจากปุ่ม Save & Apply ด้านล่าง" หรือทางเลือกที่แรงกว่า
คือ disable/ซ่อน footer "Save & Apply"+"Revert unsaved changes" ทั้งคู่ทันทีที่
sidebar อยู่ใน section CORE V2 (เหมือนที่ Routing/Migration ไม่มีปุ่ม Save เลย
เพราะ read-only ล้วน)

### 2. "Dry-run" ใช้ `gold_button` (primary CTA) ทั้งที่เป็นแค่ simulation
`settings_core_v2.py:1090` — `Refresh (inspect + plan)` = `secondary_button`,
`Dry-run` = `gold_button` ทั้งที่ report ยืนยันเองว่าไม่มีปุ่ม apply ในหน้านี้เลย
("apply ทำผ่าน CLI เท่านั้น") ตาม design system นี้ gold สงวนไว้กับ action ที่
"เขียนของจริง" (Save flags/Save policy ก็ gold ด้วยเหตุผลนั้น) — Dry-run ไม่เขียน
อะไรเลย แค่ preview เหมือน Refresh ทุกประการ การให้มันเด่นกว่า Refresh ด้วย gold
เสี่ยงให้ผู้ใช้อ่านว่า "นี่คือปุ่มสำคัญ/ปุ่มที่ใกล้เคียง apply ที่สุด" ทั้งที่จริงๆ
ทั้งคู่ปลอดภัยเท่ากัน

**Impact: med**
**แก้:** เปลี่ยน `Dry-run` เป็น `secondary_button` เหมือน `Refresh` (ทั้งคู่เป็น
read action ระดับเดียวกัน) — เก็บ gold ไว้เฉพาะปุ่มที่เขียนสถานะจริงเท่านั้น
ทั่วทั้งแอป

### 3. Brain → "Search (recall)" results list ไม่มี empty-state text ก่อนค้นครั้งแรก
`_cv2_brain_results` (`QListWidget`, บรรทัด 806-808) ไม่มี placeholder item ใดๆ
ตอนหน้าเพิ่งเปิด — เห็นเป็นกล่องขอบว่างเปล่าเฉยๆ ในสกรีนช็อต (ต่างจากทุก empty
state อื่นในหน้าเดียวกัน/หน้าอื่นๆ ใน 6 หน้านี้ที่มีข้อความไทยบอกชัดว่าต้องกดอะไร
ก่อน) หลังค้นแล้วไม่เจอผลมีข้อความ "(ไม่พบผลลัพธ์...)" อยู่แล้ว (บรรทัด 861) —
กรณีที่ขาดคือ **ก่อน**กด Search ครั้งแรกเท่านั้น

**Impact: low-med**
**แก้:** เพิ่ม 1 บรรทัดตอนสร้าง view: `results.addItem("พิมพ์คำค้นแล้วกด Search
เพื่อดูผลลัพธ์")` แล้ว `.clear()` ทันทีที่เริ่มค้นจริง (ตาม pattern ที่บรรทัด 845
ทำอยู่แล้ว)

## 💡 Nice-to-have

1. **Accounts/Pools list เหลือพื้นที่ว่างเยอะเมื่อมีแค่ 1 แถว** —
   `setMaximumHeight(160)` (บรรทัด 487, 515) แก้จาก "สูงเกินไป" ตาม self-review
   ใน report แล้ว แต่ยังเหลือ ~120px ว่างเปล่าใต้แถวเดียวในสกรีนช็อต
   `phase9-core-v2-accounts.png` — พิจารณา size-to-content (เช่น
   `setUniformItemSizes(True)` + คำนวณสูงจาก `count()*rowHeight` ไม่เกิน cap)
   แทน fixed max-height คงที่
2. **`font-size: 12px` เป็น literal ซ้ำ 4 จุด** (บรรทัด 444, 968, 1034, 1099) —
   ไม่มี precedent เดิมใน `settings_window.py` ให้เทียบ (เป็น pattern ใหม่จริงๆ
   สำหรับ mono report/list) และ `cockpit_theme.py` ไม่มี font-size token เลย —
   ถ้า Core V2 จะมีหน้าสไตล์ report/log แบบนี้เพิ่มอีก ควรตั้งเป็นค่าคงที่ใน
   `cockpit_theme.py` (เช่น `FONT_SIZE_MONO_SM = 12`) ตอนนี้ที่ยังกระจุกอยู่ไฟล์
   เดียว ก่อนที่ "12px" จะหลุดไปเป็น magic number ที่อื่น
3. **Icon ซ้ำกับ section เดิม 4/6 ไอคอน** (target=Providers&Roles, user=Users,
   grid=MCP/Plugins/Performance ×3, star=Skill Catalog/Matrix ×2) — เป็น gap
   เดิมที่ report ยอมรับเองแล้ว (`static/icons/nav/` มีแค่ 6 ชื่อ ไม่ได้เพิ่ม
   asset ใหม่) ไม่ใช่ regression จาก PR นี้ แค่ทำให้การชนกันเยอะขึ้น — priority ต่ำ

## 🚩 Heuristic violations (Nielsen)

- **#1 Visibility of system status** (should #1) — footer save state ไม่สะท้อน
  ว่า Core V2 มี unsaved change หรือไม่ ผู้ใช้ไม่มีทางรู้จาก UI ว่า "Save & Apply"
  จะไม่ทำอะไรกับ flag ที่เพิ่ง toggle
- **#4 Consistency and standards** (should #2) — gold ใช้ไม่ตรงความหมายเดิมของ
  ระบบ (primary write action) บนปุ่มที่ไม่เขียนอะไร
- **#1 Visibility of system status** (should #3) — กล่องผลลัพธ์ว่างเปล่าไม่บอก
  ผู้ใช้ว่าต้องทำอะไรต่อ ต่างจากทุกจุดอื่นในหน้าเดียวกัน

## 🎯 Recommended next steps (สำหรับ Lead)

1. [should] มอบ frontend แก้ 3 ข้อ should ข้างบนใน `settings_core_v2.py` — งานเล็ก
   ไม่แตะ core logic เลย (เปลี่ยน button class 1 บรรทัด + เพิ่ม hint/placeholder
   text) ทำพร้อมกันได้ใน 1 รอบ
2. [nice] ถ้ามีเวลา แก้ nice #1 (list height) พร้อมกันในรอบเดียวกับ should
3. [nice] nice #2/#3 พับเก็บไว้เป็น follow-up ไม่เร่ง — ไม่กระทบผู้ใช้จริงตอนนี้
