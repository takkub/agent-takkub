---
date: 2026-07-11
project: agent-takkub
role: codex
topic: Skill/Role separation + role colors + UI token migration + task wrap cross-check
---

# Codex cross-check: skill/role migration

## สรุป

| ข้อ | ผล | สรุป |
|---|---|---|
| 1. Role Overlap / Skill Catalog | **ไม่ผ่าน (minor)** | ตัว view แยกจริงและ test ผ่าน แต่ยังมี stale concept ใน `pane_tools_dialog.py` และการหา role ที่อ้าง skill ใช้ substring จึงรายงาน false positive ได้ |
| 2. Role-color single source | **ผ่าน** | `cockpit_theme.ROLE_COLORS` ตรงกับ `roles.py` สำหรับ built-in ทุกตัว; สี grid เปลี่ยน 8 role ตรงตามที่อ้าง |
| 3. Color/font migration | **ผ่าน (ตาม scope ไฟล์ที่ระบุ)** | ไม่พบ runtime hardcode ของ palette เก่าในไฟล์ migration; semantic state colors ยังเป็น state/provider tokens ไม่ถูกแปลงเป็น gold |
| 4. Task dock word-wrap | **ผ่าน** | delegate แก้ที่ `sizeHint()` ถูกจุด, row สั้น/ว่างไม่โป่ง และ smoke benchmark ไม่ชี้ regression ด้าน performance |

## 1) Role Overlap กับ Skill Catalog — ไม่ผ่าน (minor)

สิ่งที่ผ่าน:

- view index 0–7 ยังอยู่ตำแหน่งเดิม; old page index 5 กลายเป็น `VIEW_ROLE_OVERLAP` และ real catalog append ที่ index 8 (`src/agent_takkub/settings_window.py:124`, `:127`, `:464-475`)
- sidebar แยก ROLE / TOOLS / SKILL จริง (`settings_window.py:133-143`)
- Role Overlap ยังใช้ `skill_audit.load_all_role_docs()` + `audit_existing_role()` ส่วน Skill Catalog ใช้ `skill_scan.scan_skills()` (`settings_window.py:1811-1862`, `:1877-1884`)
- targeted UI tests ครอบคลุมการเลือก role, skill description/referencing roles และ empty catalog (`tests/test_settings_window.py:444-512`) และผ่าน

ปัญหาที่พบ:

1. `src/agent_takkub/pane_tools_dialog.py:3-7` ยังบอกว่า Settings “Team”/policy view ประกอบด้วย `VIEW_SKILL_CATALOG` ทั้งที่ catalog ใหม่อยู่ section SKILL และไม่ใช่ policy helper consumer ข้อความนี้ทำให้ concept ที่เพิ่งแยกกลับมาปนกันใน source documentation ควรเปลี่ยนเป็น MCP/Plugins/Providers-Roles หรือเอา Skill Catalog ออก
2. `src/agent_takkub/settings_window.py:1934-1940` ใช้ `skill_name.lower() in doc.lower()` เพื่อสรุปว่า role “อ้างถึง” skill; ชื่อสั้นหรือคำทั่วไป (เช่น `git`, `test`, `review`) สามารถ match prose ธรรมดาโดยไม่ได้อ้าง skill จริง จึงทำให้ catalog แสดง role false positive ได้ Test ปัจจุบันมีเฉพาะ positive case (`tests/test_settings_window.py:470-504`) ไม่มี negative boundary case แนะนำ match รูปแบบ reference ที่ `_append_skill_references()` สร้าง หรืออย่างน้อยใช้ boundary/escaped regex

ไม่พบ view แตกจากการย้าย stack: constants, headers, nav indicators และ initial-view tests สอดคล้องกัน

## 2) Role-color single source — ผ่าน

- canonical map อยู่ที่ `src/agent_takkub/cockpit_theme.py:184-206`; `roles.py` mirror literal เพื่อคง pure-leaf (`src/agent_takkub/roles.py:27-33`)
- guard tests เช็กทั้ง equality และ coverage ของ built-in (`tests/test_role_registry_sync.py:106-131`) และผ่าน
- เทียบกับ HEAD เดิมแล้วเปลี่ยน **8 role ตรงตามรายงาน**:
  - lead `#f5c542 -> #E3B341`
  - frontend `#22d3ee -> #34B7AC`
  - backend `#3b82f6 -> #4E86F7`
  - mobile `#a855f7 -> #A472F0`
  - devops `#22c55e -> #43B562`
  - qa `#f97316 -> #E39A3C`
  - reviewer `#ef4444 -> #F26D6D`
  - critic `#ec4899 -> #F0619A`
- codex/gemini/shell ไม่เปลี่ยน และถูกเติมเข้า canonical map; `AgentPane` ใช้ `ROLE_COLORS.get(role.name, role.color)` ทำให้ custom role ยัง fallback สีของตนเอง (`src/agent_takkub/agent_pane.py:207`)
- import-linter ผ่าน 18/18 contracts จึงไม่ทำให้ pure-leaf/UI layering พัง

## 3) Color/font migration — ผ่านตาม scope

- scan ไฟล์ที่ระบุใน migration ไม่พบ runtime hex ของ indigo/blue/green/neutral palette เก่า เหลือเพียง comment บอกค่าเดิมที่ `project_tab.py:32`, `update_panel.py:415`, `user_actions.py:440`
- primary/selected actions เปลี่ยนเป็น gold token; status/error/success/provider colors ยังคง semantic meaning ผ่าน `STATE_*`, `BANNER_*`, `PROVIDER_*`, `CHIP_*`, `METER_*`
- font hardcode ของ diagnostics report เปลี่ยนไปใช้ `ensure_fonts_loaded()["mono"]` (`src/agent_takkub/user_actions.py:399-408`); Role Overlap ใช้ mono family จาก shared font map (`settings_window.py:1845`)
- `token_meter.py` ยังเป็นข้อยกเว้น pure-leaf ตามเอกสาร และ import-linter ยืนยันว่าไม่มี Qt dependency รั่วเข้า engine

หมายเหตุ:

- เอกสารเรียกงานนี้ว่า “12 ไฟล์” แต่ตารางระบุ source UI **13 ไฟล์** เมื่อนับ `usage_meter.py` และ `limit_panel.py` แยกกัน รวม `settings_window.py`; เป็น count/documentation mismatch ไม่ใช่ runtime defect
- นอก scope รายชื่อดังกล่าวยังมี neutral/blue hardcode เก่า เช่น `claude_auth_dialog.py:38,72,84`, `remote/settings_dialog.py:267,315,319,363,370`, `terminal_widget.py:693` ถ้าคำว่า “UI ทั้ง cockpit” ในเอกสารหมายถึงทั้ง repository จริง ควรมี follow-up audit; ไม่ถือเป็น failure ของ scope 12/13 ไฟล์ที่ task ระบุ

## 4) Task dock word-wrap — ผ่าน

- root fix อยู่ตรงจุด: `_WrapItemDelegate.sizeHint()` วัด `QFontMetrics.boundingRect(..., TextWordWrap, ...)` ตาม viewport/indentation แล้วคืนความสูงมากกว่า base เฉพาะ row ที่มี text (`src/agent_takkub/task_dock.py:134-173`)
- tree ปิด uniform row heights และติด delegate จริง (`task_dock.py:290-295`)
- empty project row fall through base size hint จึงไม่ชน `ProjectCardWidget`; short row ใช้ `max(base, wrapped + pad)` จึงไม่หดหรือทับ
- tests ครอบคลุม delegate installation, long > short และ empty == base (`tests/test_task_dock.py:267-322`) และผ่าน
- offscreen smoke ที่ viewport 358px ได้ short 18px / long 74px; เรียก `sizeHint()` 5,000 rows ใช้ประมาณ 141 ms (~0.028 ms/row) บนเครื่องตรวจ ไม่เห็น performance regression ที่มีนัยสำคัญสำหรับ tree ขนาดปกติ การคำนวณเป็น O(depth) และไม่มี allocation/cache ที่โตตามจำนวน item

## Verification ที่รัน

- `pytest -q tests/test_settings_window.py tests/test_role_registry_sync.py tests/test_task_dock.py tests/test_main_window_status_bar.py` — **115 passed**
- `ruff check` บน source/test ที่เปลี่ยนทั้งหมด — **ผ่าน**
- `lint-imports` — **18 kept, 0 broken**
- repo scans: view-constant usages, role-color call sites, old palette literals, font-family literals และ diff เทียบสี role ก่อน/หลัง

## ข้อเสนอแนะก่อน merge

แก้สอง minor ในข้อ 1 ก่อน: stale doc ที่ `pane_tools_dialog.py:3-7` และทำ reference detection ให้ไม่ใช่ raw substring พร้อม negative test แล้วชุดงานนี้จะผ่านทั้ง 4 ข้อโดยไม่มีข้อค้าง
