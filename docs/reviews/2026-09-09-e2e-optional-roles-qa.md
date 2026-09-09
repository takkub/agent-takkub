# E2E QA Report: Live Verification of Optional Roles (2.0.5)

- **Date:** 2026-09-09
- **Role:** QA Specialist (`qa`)
- **Target Instance:** Live Dev Cockpit (`PID 29200`, `HWND 45420456`, title `agent-takkub [dev · agent-takkub] — dev team cockpit - agent-takkub`, port `53692`, DATA_HOME `C:\Users\monch\WebstormProjects\agent-takkub`)
- **Prod Instance Guard:** Verified untouched (`PID 31728`, `HWND 63835628`, title `agent-takkub v2.0.4 — dev team cockpit - agent-takkub`, port `49263`, DATA_HOME `C:\Users\monch\.agent-takkub`)
- **Commits / Features Verified:** `ca1c737`, `dc78cc0`, `36016b6` (Register 5 secondary roles: tester, analyst, designer, docs, security; Settings → Roles & ตำแหน่ง roster toggle; Pipeline hop editor palette)

---

## Executive Summary

| # | Checklist Item | Status | Evidence / Notes |
|---|---|:---:|---|
| **1** | **Settings → Roles & ตำแหน่ง** (Dark / Light)<br>- แถว Tester/Analyst/Designer/Docs/Security ใหม่ toggle ปิดอยู่ (default)<br>- สีตรงกับ `cockpit_theme.ROLE_COLORS`<br>- Badge "4/9 active" ถูกต้อง | **PASS** | `01_settings_roles_dark.png`, `01_settings_roles_light.png`<br>ทั้ง 5 roles แสดงผลถูกต้อง สีตรงเป๊ะ (`#B5D33D`, `#45C4D6`, `#C77DF0`, `#8FA3B8`, `#E0574F`) และ badge แสดง `4/9 active` |
| **2** | **เปิด toggle "Tester" จริง**<br>- กด Save & Apply แล้วยืนยันว่า role เปิดใช้งานจริง<br>- Badge เปลี่ยนเป็น "5/9 active"<br>- Toggle ยังอยู่สถานะเปิดหลัง reload หน้า | **PASS** | `02_settings_tester_enabled_dark.png`<br>Toggle Tester เปิด (`checked=True`), บันทึก `team_preset.json` + `pipelines.json` สำเร็จ, reload หน้าต่างใหม่ toggle ยังคงเปิด และ badge เปลี่ยนเป็น `5/9 active` |
| **3** | **`takkub assign --role tester` สดจริง**<br>- Spawn Tester จริงบน dev cockpit<br>- ไม่ error "unknown role"<br>- รัน `pytest tests/test_roles.py -q` จนจบ ปิดตัวเองปกติ | **PASS** | Task `134818-tester.md`, Done `tester-134953.md`<br>Spawn ติด (`PID 32988`), รัน pytest 22 passed / 0 failed, ส่ง done event และปิด pane อัตโนมัติ |
| **4** | **Settings → Pipeline hop editor**<br>- Chip "Tester" (และ Analyst/Designer/Docs/Security) เลือกได้ใน palette<br>- ลองเพิ่มเข้า HOP 3 ทดสอบ 1 ครั้ง<br>- ยืนยันว่า save ได้จริง | **PASS** | `04_pipeline_builder_tester_hop_dark.png`<br>พบทั้ง 5 roles ใน `_pipeline_palette_roles()`, เพิ่ม Tester เข้า HOP 3 สำเร็จ, save เข้า `pipelines.json` ถูกต้อง (คืนค่ากลับสู่ default เดิมหลังทดสอบ) |
| **5** | **ปิด toggle กลับ**<br>- ปิด Tester toggle กลับเป็นปิด (คืนสภาพเดิม)<br>- ยืนยัน `takkub assign --role tester` ถูกปฏิเสธ/บล็อกตามเดิม | **PASS** | `05_settings_tester_reverted_off_dark.png`<br>Badge กลับเป็น `4/9 active`, Tester toggle ปิด, ทดสอบ `takkub assign --role tester` ถูกบล็อกทันทีด้วยข้อความ `err: role tester ถูกปิดใน Settings ของโปรเจคนี้ (agent-takkub)` |
| **6** | **เช็คซ้ำจากรอบก่อน**<br>- Settings → Usage: Gemini quota ไม่มีแถวซ้ำ 25+ แถว<br>- Skills Catalog ไม่ blank canvas | **PASS** | `06_settings_usage_dark.png`, `06_settings_skills_catalog_dark.png`<br>Usage แสดงตารางสวยงามไม่มีแถวซ้ำ; Skills Catalog แสดงรายการ skill ทั้ง 7 พร้อมรายละเอียดฝั่งขวาครบถ้วน |

---

## Visual & Log Evidence

1. **Item 1:**
   - Dark mode screenshot: `runtime/exports/2026-09-09/agent-takkub/screenshots/01_settings_roles_dark.png`
   - Light mode screenshot: `runtime/exports/2026-09-09/agent-takkub/screenshots/01_settings_roles_light.png`
   - Role colors verified against `cockpit_theme.ROLE_COLORS`:
     - `tester`: `#B5D33D`
     - `analyst`: `#45C4D6`
     - `designer`: `#C77DF0`
     - `docs`: `#8FA3B8`
     - `security`: `#E0574F`
   - Header badge: `4/9 active` under preset `full` ("ทีมเต็ม").

2. **Item 2:**
   - Screenshot with Tester enabled: `runtime/exports/2026-09-09/agent-takkub/screenshots/02_settings_tester_enabled_dark.png`
   - Presisted JSON in `C:\Users\monch\.takkub\projects\agent-takkub\team_preset.json`:
     `"roles": {"frontend": true, "backend": true, "mobile": true, "devops": true, "tester": true, "analyst": false, "designer": false, "docs": false, "security": false}`
   - Header badge: `5/9 active`

3. **Item 3:**
   - Task prompt: `runtime/tasks/agent-takkub/2026-09-09/134818-tester.md` (`รัน pytest tests/test_roles.py -q`)
   - Done summary: `runtime/sessions/2026-09-09/agent-takkub/tester-134953.md`
   - Test execution result: `pytest tests/test_roles.py -q: 22 passed, 0 failed`
   - Auto-close: Pane terminated cleanly within 2.5s after done signal.

4. **Item 4:**
   - Screenshot with Tester in HOP 3: `runtime/exports/2026-09-09/agent-takkub/screenshots/04_pipeline_builder_tester_hop_dark.png`
   - Saved into `pipelines.json` template `feature`, then reverted to original clean state.

5. **Item 5:**
   - Screenshot with Tester reverted to OFF: `runtime/exports/2026-09-09/agent-takkub/screenshots/05_settings_tester_reverted_off_dark.png`
   - Assign block stderr/stdout:
     `err: role tester ถูกปิดใน Settings ของโปรเจคนี้ (agent-takkub) — เปิดที่ Providers & Roles หรือใช้ role อื่น`

6. **Item 6:**
   - Screenshot Usage page: `runtime/exports/2026-09-09/agent-takkub/screenshots/06_settings_usage_dark.png`
   - Screenshot Skills Catalog: `runtime/exports/2026-09-09/agent-takkub/screenshots/06_settings_skills_catalog_dark.png`

---

## Bugs Identified (แยกจาก Opinion เพื่อให้ Lead พิจารณาเปิดใบงาน)

### Bug 1: Preset "auto" ใน `team_preset._resolve()` ยังคงเปิดตำแหน่งเสริมทั้ง 5 เป็น True โดยปริยาย
- **ไฟล์:** `src/agent_takkub/team_preset.py:276`
- **โค้ด:**
  ```python
  # "auto": no fixed roster — advisory only (see routing_planner.suggest_team_size).
  return {
      "preset": "auto",
      "roles": dict.fromkeys(_position_roles(project), True),
      "checker": "qa",
      "lead_may_implement": False,
      "template": "feature",
      "exec_mode": "parallel",
  }
  ```
- **พฤติกรรมที่พบ:** `_position_roles(project)` รวม `EXTRA_POSITION_ROLES` เข้าไปด้วย เมื่อโปรเจกต์อยู่ใน preset `auto` (ค่า default ก่อนเซ็ต preset) ฟังก์ชัน `dict.fromkeys(..., True)` จะเปิดทุกตำแหน่งเป็น `True` ทั้งหมด ทำให้ตำแหน่งเสริม (tester, analyst, designer, docs, security) ถูกเปิดเป็น default และ badge ใน Settings กลายเป็น `9/9 active`
- **ผลกระทบ:** ขัดกับ `CHANGELOG.md` ("ปิดอยู่โดย default ทุก preset รวม 'ทีมเต็ม'") และ `CLAUDE.md` ("secondary positions, off by default")
- **แนวทางแก้ไขที่แนะนำ:** ใน branch `auto` ควรกำหนดให้ `CORE_POSITION_ROLES` เป็น `True` และ `EXTRA_POSITION_ROLES` เป็น `False` เช่นเดียวกับ preset `full`

### Bug 2: เมื่อโปรเจกต์อยู่ใน preset "custom" แล้ว การแก้ toggle ราย role แล้วกด Save & Apply ไม่บันทึกลง `team_preset.json`
- **ไฟล์:** `src/agent_takkub/team_preset.py:363-364`
- **โค้ด:**
  ```python
  def note_manual_roles_change(new_roles_enabled: dict, project: str | None = None) -> bool:
      preset_id = current_preset_id(project)
      if preset_id in ("custom", "auto"):
          return False
      ...
  ```
- **พฤติกรรมที่พบ:** 
  1. เมื่อผู้ใช้เปิด toggle `tester` เป็น ON จากหน้า Settings หน้าจอจะเปลี่ยน preset เป็น `custom` และเซฟ `team_preset.json["custom"]["roles"]["tester"] = True`
  2. แต่เมื่อผู้ใช้กลับมาปิด toggle `tester` ให้กลับเป็น OFF ในครั้งต่อไป `note_manual_roles_change` จะเข้าเงื่อนไข `if preset_id in ("custom", "auto"): return False` ทันที ทำให้ไม่มีการอัปเดตค่า `custom["roles"]` ใน `team_preset.json`
  3. เมื่อเปิด Settings ขึ้นมาใหม่ `team_preset.resolve("custom")` จะอ่านค่าเก่าที่ค้างอยู่ใน `team_preset.json` ทำให้ toggle `tester` เด้งกลับมาเปิดเป็น ON อีกครั้ง (ผู้ใช้ต้องคลิกที่การ์ด preset "ทีมเต็ม" ถึงจะรีเซ็ตสถานะได้)
- **แนวทางแก้ไขที่แนะนำ:** ใน `note_manual_roles_change` หาก `preset_id == "custom"` ควรทำการ update `custom["roles"]` ใน `team_preset.json` ตาม `new_roles_enabled` แทนที่จะข้าม
