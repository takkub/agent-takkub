# Design Sweep Index (2026-07-20)

## User Pain Context
"หา feature ไม่เจอ (ช่อง Model อยู่หน้าต่างนึง แต่เปิดอีกหน้าต่างไม่มี) · Settings ซ้ำซ้อน 2 หน้าต่าง (👥 Team ใหม่ vs legacy Pipeline Settings) · copy ล้าสมัย · โดยรวมไม่มีความสุขในการใช้งาน"

## ภาพรวม UI Surfaces

| Path | หน้า UI | มองเห็นอะไร (1 บรรทัด) |
| :--- | :--- | :--- |
| `docs/qa-reports/design-sweep/01-main-window.png` | Main Window | หน้าต่างหลักแสดง tab strip และ pane grid รวมๆ |
| `docs/qa-reports/design-sweep/01-main-window-status-bar.png` | Main Window (Status Bar) | แถบ Status Bar ด้านขวาแบบซูมชัด |
| `docs/qa-reports/design-sweep/02-team-roles.png` | 👥 Team (New) -> Roles | รายการ Roles ด้านซ้าย และรายละเอียดด้านขวา |
| `docs/qa-reports/design-sweep/02-team-skills.png` | 👥 Team (New) -> Skills | รายการ Skills Catalog ในหน้าต่างการตั้งค่าใหม่ |
| `docs/qa-reports/design-sweep/02-team-mcp-servers.png` | 👥 Team (New) -> MCP Servers | รายการจัดการ MCP Servers ในระบบใหม่ |
| `docs/qa-reports/design-sweep/02-team-plugins.png` | 👥 Team (New) -> Plugins | รายการจัดการ Plugins ในระบบใหม่ |
| `docs/qa-reports/design-sweep/02-team-users.png` | Legacy Settings -> Users | การจัดการ Claude profile และ Auth overrides |
| `docs/qa-reports/design-sweep/02-team-skill-matrix.png` | Legacy Settings -> Skill Matrix | ตาราง Grid แบบสลับการตั้งค่า Role × Skill |
| `docs/qa-reports/design-sweep/02-team-skill-catalog-legacy.png` | Legacy Settings -> Skill Catalog | รายการ Skills และคำอธิบายในมุมมองเก่า |
| `docs/qa-reports/design-sweep/03-legacy-pipeline-builder.png` | Legacy Settings -> Pipeline Builder | หน้าลาก-วาง Hop และ Role ใน Pipeline |
| `docs/qa-reports/design-sweep/03-legacy-templates.png` | Legacy Settings -> Templates | การจัดการ Pipeline Template ที่บันทึกไว้ |
| `docs/qa-reports/design-sweep/03-legacy-role-overlap.png` | Legacy Settings -> Role Overlap | การตรวจสอบ Scope overlap ของ Role (TF-IDF) |
| `docs/qa-reports/01-team-providers-full.png` | 👥 Team (New) -> Providers | รายการ Provider ทั้งหมด (ภาพเดิม) |
| `docs/qa-reports/02-team-provider-claude.png` | 👥 Team (New) -> Provider Detail | หน้าตั้งค่าของ Claude (ภาพเดิม) |
| `docs/qa-reports/02-team-provider-codex.png` | 👥 Team (New) -> Provider Detail | หน้าตั้งค่าของ Codex (ภาพเดิม) |
| `docs/qa-reports/02-team-provider-cursor.png` | 👥 Team (New) -> Provider Detail | หน้าตั้งค่าของ Cursor (ภาพเดิม) |
| `docs/qa-reports/02-team-provider-gemini.png` | 👥 Team (New) -> Provider Detail | หน้าตั้งค่าของ Gemini (ภาพเดิม) |
| `docs/qa-reports/02-team-provider-kimi.png` | 👥 Team (New) -> Provider Detail | หน้าตั้งค่าของ Kimi (ภาพเดิม) |
| `docs/qa-reports/02-team-provider-opencode.png` | 👥 Team (New) -> Provider Detail | หน้าตั้งค่าของ OpenCode (ภาพเดิม) |
| `docs/qa-reports/03-team-roundtrip-save.png` | 👥 Team (New) -> Provider Detail | การบันทึกค่า (Save) ของ Provider (ภาพเดิม) |
| `docs/qa-reports/04-team-roundtrip-clear.png` | 👥 Team (New) -> Provider Detail | การล้างค่า (Clear) ของ Provider (ภาพเดิม) |
| `docs/qa-reports/05-legacy-settings.png` | Legacy Settings | หน้าการตั้งค่า Pipeline แบบเก่า (ภาพเดิม) |
| `docs/qa-reports/06-legacy-combo-lead.png` | Legacy Settings | Dropdown สำหรับเลือก Provider (ภาพเดิม) |
| `docs/qa-reports/07-status-bar.png` | Main Window -> Status Bar | แถบ Status Bar ด้านขวา (ภาพเดิม) |

## หน้าที่ข้ามและเหตุผล
- **Context Menus, Modal Dialogs (เช่น เมนู Add User แบบ Popup, Delete confirmation)**: ข้ามเนื่องจากข้อจำกัดของ Headless QA ที่ไม่สามารถดักจับ Popup ชั่วคราวที่ต้องพึ่งพา Event loop หรือการคลิกจริงได้
- **หน้า Providers ใน 👥 Team ใหม่**: ข้ามการแคปใหม่ เนื่องจากมีภาพ 12 ใบเดิมอยู่แล้วตามคำสั่ง

## สิ่งที่สังเกตระหว่างแคป (Observations)
- **Settings ซ้ำซ้อน 2 ระบบอย่างชัดเจน**: มีหน้าต่าง `SettingsManagementWindow` ใหม่ และ `SettingsWindow` แบบ Legacy ที่ทำงานคู่ขนานกัน ทำให้ผู้ใช้ต้องจำว่าอะไรอยู่ที่ไหน
- **ความไม่ต่อเนื่องของการตั้งค่า**: ข้อมูล Users และ Skill Matrix หาได้จากหน้าต่าง Legacy เท่านั้น ในขณะที่หน้าต่างใหม่มีเพียง Roles, Skills, MCP Servers, Plugins
- **ความซ้ำซ้อนของฟีเจอร์**: "Skills" ในหน้าต่างใหม่ และ "Skill Catalog" ในหน้าต่าง Legacy เป็นเหมือนสองทางเข้าที่อาจทำให้ผู้ใช้เกิดความสับสน
- **Navigation ขาดความเชื่อมโยงที่สมูท**: ผู้ใช้ต้องเปิดปิดหน้าต่างที่แตกต่างกันเพื่อจัดการความสัมพันธ์ระหว่าง Role และ Skill หรือ Pipeline
