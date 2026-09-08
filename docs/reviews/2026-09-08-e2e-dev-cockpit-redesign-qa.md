# E2E QA Review: Cockpit UI Redesign (Rounds 1 & 2 Live Verification)

- **Date:** 2026-09-08
- **Role:** QA Specialist (`qa`)
- **Target Instance:** Live Dev Cockpit (`PID 8384`, `HWND 30347978`, title `agent-takkub [dev · agent-takkub] — dev team cockpit - agent-takkub`)
- **Prod Instance Guard:** Verified untouched (`PID 27120`, `HWND 2163668`, title `agent-takkub v2.0.0 — dev team cockpit - agent-takkub`)
- **Commits Verified:** `4fe5714`, `ba98bfe`, `19bfeb0` (Apex Slate/Titanium indigo palette, semantic colors, larger typography, Team->Settings rename, Skills Catalog collapse guard, CockpitDialog migration)

---

## Executive Summary

| Checklist Item | Result | Evidence / Notes |
|---|:---:|---|
| **1. Sidebar/nav ทั้งหมด (สี indigo ใหม่, ไร้คราบ gold)** | **PASS** | `01_dev_cockpit_main.png`, `02_settings_default_view.png`, `11_main_window_light.png`, `12_settings_team_light.png` |
| **2. Pane status dot + role-colored left border** | **PASS** | Status dots (Lead=green, QA=amber). 3px role border (`#pane_lead` = `#E3B341` gold) visible on frame. |
| **3. หน้า Settings (เดิมชื่อ Team) & ความซ้อนทับของชื่อ** | **PASS (Flagged)** | `02_settings_default_view.png`, `12_settings_team_light.png`. Section header "SETTINGS" + "Settings & ตำแหน่ง" + "SETTINGS SIZE" ซ้อนกันจริง |
| **4. ตรวจหาคำว่า "Team" ตกหล่น** | **PASS** | User-facing English "Team" ถูกเปลี่ยนเป็น "Settings" ครบทุกจุด; ภาษาไทย "ทีม" คงไว้เฉพาะ domain prose |
| **5. หน้า Usage (sparkline METER_CLAY)** | **PASS** | `04_settings_usage_dark.png`, `13_settings_usage_light.png`. Sparkline สี METER_CLAY ชัดเจน มีข้อมูลจริง |
| **6. Settings > Skills > Catalog (ไม่ blank canvas)** | **PASS** | `03_settings_skills_catalog_clear.png`, `14_settings_skills_light.png`. สลับแท็บ Catalog <-> Matrix 3 รอบ ไม่ blank |
| **7. Dialogs migrate ไป CockpitDialog (ไร้ white-bleed)** | **PASS** | `05_dialog_add_account_dark.png`, `06_dialog_remote_settings_dark.png`, `07_dialog_new_project_dark.png`, `08_dialog_role_permissions_dark.png` |
| **8. Typography scale ใหญ่ขึ้นจริง** | **PASS** | เทียบกับ `round2_before_dark_team.png` พบขนาด font หัวข้อ, kicker, label, dropdown ใหญ่ขึ้น 1-2px อ่านสบายตาขึ้นมาก |
| **9. คลิกปุ่มหลักไม่ crash / exception** | **PASS** | ทดสอบคลิก Lead/QA tabs, Collapse/Expand sidebar, Tasks, Doctor, Settings navs, Dialog openers ไม่มี crash |
| **10. สลับ Live Dark <-> Light Retheme** | **PASS** | `10_settings_general_light.png` ถึง `15_main_window_dark_restored.png`. Retheme ทำงานสดทันที ไม่มีสีตกค้าง |

---

## Detailed Findings & Visual Verifications

### 1. Sidebar/Nav Palette (Apex Slate & Titanium Indigo)
- ทั้ง Dark และ Light themes เรนเดอร์ด้วยพาเลท Apex Slate / Titanium Indigo ใหม่ 100%
- ไร้คราบสีทอง/เหลืองมัสตาร์ดเดิมในกรอบ navigation, sidebar headers, selection pills และ active indicators
- Evidence:
  - Dark main window: `runtime/exports/2026-09-08/agent-takkub/screenshots/qa-e2e/01_dev_cockpit_main.png`
  - Dark settings sidebar: `runtime/exports/2026-09-08/agent-takkub/screenshots/qa-e2e/02_settings_default_view.png`
  - Light main window: `runtime/exports/2026-09-08/agent-takkub/screenshots/qa-e2e/11_main_window_light.png`
  - Light settings sidebar: `runtime/exports/2026-09-08/agent-takkub/screenshots/qa-e2e/12_settings_team_light.png`

### 2. Pane Status Dot & Role-Colored Left Border
- Pane status dots ใน Tab bar ใช้สี semantic:
  - Lead: Green dot (`#10B981`)
  - QA: Amber working dot (`#E39A3C`)
- ขอบซ้ายของ pane (`#pane_lead`) มี vertical accent strip หนา 3px ตาม `agent_pane.py:1172` แสดงสี `#E3B341` (Lead role color) ช่วยแยกแยะ role identity โดยไม่แย่งความเด่นกับ status dot
- Evidence: `runtime/exports/2026-09-08/agent-takkub/screenshots/qa-e2e/01_dev_cockpit_main.png`

### 3. Review หน้า Settings (เดิมชื่อ Team) — จุดที่ Frontend ธงไว้
- ภาพรวมการแสดงผลจริงบน dev cockpit:
  - หน้าต่าง Settings มี Title: `Takkub Cockpit — Settings - agent-takkub`
  - ใน Sidebar มี section header: `SETTINGS`
  - รายการเมนูย่อยใต้ section: `Settings & ตำแหน่ง`
  - หัวข้อหน้าหลัก: `Settings & ตำแหน่ง`
  - Kicker ด้านบนการ์ดขนาดทีม: `SETTINGS SIZE` (เนื้อหาภายในการ์ดระบุ "ขนาดทีมของโปรเจกต์นี้", "ทีมเต็ม")
  - Kicker ด้านบนรายชื่อ role: `SETTINGS ROSTER` (เนื้อหาหัวข้อระบุ "ตำแหน่งในทีม")
- **ข้อสังเกต UX เพื่อให้ user ตัดสินใจ:**
  - การใช้คำว่า "SETTINGS" ใน section header แล้วตามด้วย "Settings & ตำแหน่ง" เกิดการซ้ำซ้อนของคำอย่างเห็นได้ชัด
  - Kicker "SETTINGS SIZE" และ "SETTINGS ROSTER" อ่านดูขัดกับธรรมชาติเล็กน้อยเมื่อเทียบกับเนื้อหาไทยที่ยังพูดถึง "ขนาดทีม" และ "ตำแหน่งในทีม"
  - **ข้อเสนอแนะ:** หาก user เห็นชอบ อาจปรับชื่อเมนูย่อยและหัวข้อหน้านี้เป็น "Roles & ตำแหน่ง" หรือ "Roles & Roster" และ section header เป็น "ROLES" เพื่อลดความซ้ำซ้อนกับชื่อหน้าต่าง Settings ใหญ่
  - Evidence:
    - Dark view: `runtime/exports/2026-09-08/agent-takkub/screenshots/qa-e2e/02_settings_default_view.png`
    - Light view: `runtime/exports/2026-09-08/agent-takkub/screenshots/qa-e2e/12_settings_team_light.png`

### 4. ตรวจสอบการตกหล่นของคำว่า "Team"
- ตรวจสอบ user-facing surface ทั้งหมด:
  - Status bar chip เปลี่ยนเป็น `ตั้งค่า: อัตโนมัติ` (เดิม "ทีม: อัตโนมัติ")
  - Status bar button เปลี่ยนเป็น `👥 Settings` (เดิม "👥 Team")
  - เมนูและหัวข้อหน้าเปลี่ยนเป็น `Settings & ตำแหน่ง`
  - หน้าต่าง Remote Control ไม่มีคำว่า Team หลงเหลือ
- คำว่า "ทีม" ในภาษาไทยยังปรากฏเฉพาะจุดที่เป็นคำอธิบาย domain prose (ขนาดทีม, ทีมเต็ม, ตำแหน่งในทีม) ตรงตามนโยบาย round 2
- ไม่พบคำว่า "Team" ภาษาอังกฤษหลงเหลือใน UI

### 5. หน้า Usage (Sparkline METER_CLAY)
- หน้า Usage แสดง sparkline กราฟแนวโน้ม 14 วัน ด้วยสี `METER_CLAY` (Terracotta / ดินเผา) ทั้งธีม Dark (`04_settings_usage_dark.png`) และ Light (`13_settings_usage_light.png`)
- สีแยกชัดเจนจาก Indigo primary CTA อย่างสวยงาม
- บน dev instance มีข้อมูล usage จริงของ Claude (`1905.9M`) และ Codex (`6.2M`) เรนเดอร์ครบถ้วน
- Evidence:
  - Dark usage: `runtime/exports/2026-09-08/agent-takkub/screenshots/qa-e2e/04_settings_usage_dark.png`
  - Light usage: `runtime/exports/2026-09-08/agent-takkub/screenshots/qa-e2e/13_settings_usage_light.png`

### 6. Settings > Skills > Catalog (Blank Canvas Guard)
- เปิดหน้า Skills > Catalog ครั้งแรกและหลังจากสลับไปหน้าอื่น แสดงรายการ skills (`cockpit-ui-style`, `debug-mantra`, etc.) ทันที
- ไม่พบบั๊ก blank canvas อีกต่อไป
- ทดสอบสลับ subtabs Catalog <-> Matrix ต่อเนื่อง 3 รอบ และสลับหน้าระหว่าง Skills กับ General/Usage ข้อมูลและ canvas ยังคงแสดงผลถูกต้องสมบูรณ์
- Evidence:
  - Dark: `runtime/exports/2026-09-08/agent-takkub/screenshots/qa-e2e/03_settings_skills_catalog_clear.png`
  - Light: `runtime/exports/2026-09-08/agent-takkub/screenshots/qa-e2e/14_settings_skills_light.png`

### 7. Dialogs Migration to CockpitDialog (White-Bleed Check)
- ทดสอบเปิด dialogs สำคัญจริงบน dev instance ในธีม Dark:
  1. **Add Account Dialog:** `05_dialog_add_account_dark.png` — พื้นหลังสีเข้ม `#111620`, input box สีกรมท่า, ขอบโฟกัส indigo, ไร้ white bleed
  2. **Remote Settings Dialog:** `06_dialog_remote_settings_dark.png` — สีพื้นหลังและคอนโทรลกลมกลืนตามธีมมืดทั้งหมด
  3. **New Project Dialog:** `07_dialog_new_project_dark.png` — สีพื้นหลังธีมมืดถูกต้อง
     - *Minor Layout Finding:* ปุ่มทั้ง 4 แถวล่าง (`เปิดโปรเจคที่...`, `สร้างโปรเจคใหม่...`, `Import โฟลเดอร์...`, `ยกเลิก`) มีความกว้างแน่นเกินไป ทำให้ข้อความภาษาไทยถูก clip บางส่วน
  4. **Role Permissions Dialog:** `08_dialog_role_permissions_dark.png` — ตารางสิทธิ์ permissions เรนเดอร์เป็นธีมมืด checkbox สวยงาม ไม่มี white bleed
  5. **Map Paths & Rules Editor:** ตรวจสอบโค้ด `project_wizard.py:309, 494` สืบทอด `CockpitDialog` ถูกต้อง

### 8. Typography Scale Inspection
- เปรียบเทียบ screenshot จริง (`02_settings_default_view.png`) กับก่อนหน้า (`round2_before_dark_team.png`):
  - ตัวหนังสือส่วน kickers (`SETTINGS SIZE`, `SETTINGS ROSTER`) เพิ่มขนาดและตัวหนาขึ้น ชัดเจนกว่าเดิมมาก
  - ชื่อตำแหน่ง Role (`Lead`, `Frontend`, `Backend`, `Mobile`) อ่านง่ายสบายตาขึ้น
  - Dropdowns (`Claude`, `(default)`, `high`) มีขนาดฟอนต์ 13px เหมาะสมกับการใช้งานจริง

### 9. Primary Actions & Crash-Free Verification
- ทดสอบการกดใช้งานคอนโทรลหลักสดบน instance dev:
  - สลับ Tab `QA` -> `Lead`
  - ยุบ/ขยาย Sidebar (`« Collapse` / `» Expand`)
  - เปิด/ปิด `📋 Tasks` dock
  - เปิด/ปิด `🩺 Doctor`
  - เมนูใน Settings ทุกหน้า
- ตรวจสอบ `runtime/boot.log` และ `runtime/events.log`: ไม่มี RuntimeError หรือ unhandled exception ใดๆ เกิดขึ้น

### 10. Live Retheme (Dark <-> Light)
- สลับธีม Dark -> Light -> Dark ขณะเปิดใช้งานจริง:
  - Main window และ Settings window ปรับเปลี่ยนสไตล์ทันทีแบบ live โดยไม่ต้อง restart
  - Tab dots, status chips, tunnel indicators เปลี่ยนโทนสีถูกต้อง
  - ไม่พบปัญหา frozen-token-snapshot bug หรือสีเก่าค้าง
  - คืนสถานะกลับสู่ Dark theme เรียบร้อยสมบูรณ์
- Evidence: `10_settings_general_light.png`, `11_main_window_light.png`, `15_main_window_dark_restored.png`

---

## Directory of Evidence Screenshots
All screenshot artifacts are preserved under:
`runtime/exports/2026-09-08/agent-takkub/screenshots/qa-e2e/`
- `01_dev_cockpit_main.png` — Dev Cockpit Main Window (Dark)
- `02_settings_default_view.png` — Settings & Roles Default View (Dark)
- `03_settings_skills_catalog_clear.png` — Skills Catalog View (Dark)
- `04_settings_usage_dark.png` — Usage View with METER_CLAY Sparkline (Dark)
- `05_dialog_add_account_dark.png` — Add Account Dialog (Dark)
- `06_dialog_remote_settings_dark.png` — Remote Control Dialog (Dark)
- `07_dialog_new_project_dark.png` — New Project Dialog (Dark)
- `08_dialog_role_permissions_dark.png` — Role Permissions Dialog (Dark)
- `09_settings_general_dark.png` — General Settings (Theme combo Dark)
- `10_settings_general_light.png` — General Settings (Theme combo Light)
- `11_main_window_light.png` — Dev Cockpit Main Window (Light)
- `12_settings_team_light.png` — Settings & Roles View (Light)
- `13_settings_usage_light.png` — Usage View with METER_CLAY Sparkline (Light)
- `14_settings_skills_light.png` — Skills Catalog View (Light)
- `15_main_window_dark_restored.png` — Dev Cockpit Main Window (Restored to Dark)
