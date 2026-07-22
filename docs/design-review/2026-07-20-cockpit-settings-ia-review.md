---
shots:
  - docs/qa-reports/05-legacy-settings.png
  - docs/qa-reports/01-team-providers-full.png
  - docs/qa-reports/design-sweep/02-team-roles.png
  - docs/qa-reports/design-sweep/02-team-skills.png
  - docs/qa-reports/design-sweep/02-team-skill-catalog-legacy.png
  - docs/qa-reports/design-sweep/02-team-skill-matrix.png
  - docs/qa-reports/design-sweep/01-main-window-status-bar.png
---
# Cockpit Settings Information Architecture Review

จากการวิเคราะห์ปัญหาผู้ใช้ (Pain points) ระดับ Information Architecture (IA) ที่พบว่ามีการสับสนในการหาจุดตั้งค่า (Model / Settings ซ้ำซ้อน / copy ล้าสมัย) ขอเสนอแนวทางปรับปรุงดังนี้:

## 1. High Impact: Consolidate Provider & Role Settings (SSOT)
- **ปัญหา:** หน้าต่างตั้งค่าซ้ำซ้อนกันอย่างชัดเจน มีหน้าต่าง `SettingsManagementWindow` (Team แบบใหม่ - PyQt6) และ `SettingsWindow` (Legacy - HTML/QWebEngine) โดยทั้งคู่มีส่วนควบคุม Providers & Roles ทำให้ผู้ใช้สับสนว่าควรตั้งค่าที่ไหน ("Settings ซ้ำซ้อน 2 หน้าต่าง")
- **หลักฐาน:** `docs/qa-reports/05-legacy-settings.png`, `docs/qa-reports/01-team-providers-full.png`, `docs/qa-reports/design-sweep/02-team-roles.png`
- **ข้อเสนอที่ทำได้จริง:** 
  1. เนื่องจากเป็นคนละ Stack (HTML vs PyQt) ไม่ควรทำ UI ซ้ำซ้อน ให้ **De-duplicate** แท็บการตั้งค่าที่เกี่ยวข้องกับ Providers และ Roles ในหน้าต่าง Legacy 
  2. เปลี่ยนแท็บนั้นใน HTML เป็นหน้าเปล่า (Placeholder) ที่มีข้อความอธิบายชัดเจนพร้อม **ปุ่ม CTA (Call to Action)** เช่น "Manage Providers & Roles" 
  3. เมื่อคลิกปุ่ม ให้ยิง JS Signal ข้าม Bridge มาฝั่ง PyQt6 เพื่อปิด/ซ่อนหน้าต่าง Legacy และเปิดหน้าต่าง `👥 Team` โดย focus ไปที่แท็บที่เกี่ยวข้องทันที
- **Effort คร่าวๆ:** Low - Medium (ลบ HTML/UI เดิม และเพิ่ม JS bridge signal ง่ายกว่าการเขียน UI ใหม่ทั้งหมด)

## 2. High Impact: Update Legacy Copy & Simplify Terminology
- **ปัญหา:** ผู้ใช้รู้สึกไม่ดีกับระบบ ("ไม่มีความสุขในการใช้งาน") ส่วนหนึ่งมาจากความล้าสมัยของเนื้อหา เช่น copy ระบุว่า "เปิด/ปิด provider (codex/gemini)" ซึ่งไม่สะท้อนความจริงที่มี 6 providers แล้ว
- **หลักฐาน:** `docs/qa-reports/05-legacy-settings.png`
- **ข้อเสนอที่ทำได้จริง:** 
  1. ปรับปรุง Copy ในไฟล์ HTML ของ legacy ให้เป็นคำกลางๆ เช่น "Manage AI Providers" หรืออัปเดตรายชื่อให้ครบ
  2. (แนะนำ) หากดำเนินการตามข้อ 1 (High Impact) แล้ว ปัญหานี้จะหายไปเองเพราะหน้าดังกล่าวจะถูกลดบทบาทลงเหลือแค่ปุ่ม Link ไปยังหน้า Team
- **Effort คร่าวๆ:** Low (แก้ไขข้อความใน HTML Template)

## 3. Medium Impact: Merge Skills & Skill Catalog Entry Points
- **ปัญหา:** มีทางเข้า Skills ซ้ำซ้อน คือ "Skills" ในหน้าต่าง Team ใหม่ และ "Skill Catalog / Skill Matrix" ในหน้าต่าง Legacy ทำให้เกิด Cognitive Load ต้องจำว่าจัดการ Skill ควบคุมที่ไหน
- **หลักฐาน:** `docs/qa-reports/design-sweep/02-team-skills.png`, `docs/qa-reports/design-sweep/02-team-skill-catalog-legacy.png`, `docs/qa-reports/design-sweep/02-team-skill-matrix.png`
- **ข้อเสนอที่ทำได้จริง:** 
  1. ทยอย Migrate หน้า Skill Catalog ออกจาก Legacy ให้อยู่ในหน้า Team (PyQt widgets) แบบเต็มตัว
  2. ระหว่างเปลี่ยนผ่าน ให้ทำลักษณะเดียวกับข้อ 1 คือวางปุ่มบนหน้า Legacy เพื่อ Redirect ผู้ใช้มายังหน้า Team สำหรับการจัดการ Skills ทั่วไป 
  3. สำหรับ "Skill Matrix" ที่อาจจะซับซ้อนเกินกว่าจะย้ายทันที ให้เก็บไว้ใน Legacy ก่อน แต่เพิ่มป้ายกำกับให้ชัดเจนว่าสำหรับการดูความสัมพันธ์เท่านั้น (Read-only view)
- **Effort คร่าวๆ:** Medium (เพิ่ม PyQt UI บางส่วน และทำ Routing ข้ามหน้าต่าง)

## 4. Low Impact: Status Bar Provider Quick Access / Discoverability
- **ปัญหา:** ผู้ใช้ "หา feature ไม่เจอ" ว่าเลือก Model หรือ Provider ตรงไหน (เพราะไปซ่อนอยู่ใน Team)
- **หลักฐาน:** `docs/qa-reports/design-sweep/01-main-window-status-bar.png`
- **ข้อเสนอที่ทำได้จริง:** 
  1. ที่แถบ Status Bar ด้านขวาล่างของ Main Window (ซึ่งปัจจุบันแสดงสถานะอยู่แล้ว) ควรทำ Widget ให้สามารถคลิกได้ (Clickable PyQt Widget) 
  2. เมื่อคลิกที่ Provider Status ให้โผล่หน้าต่าง Team -> แท็บ Providers หรือมี Popup Menu เล็กๆ เพื่อ Quick Switch ทันที เพื่อแก้ปัญหาการค้นหาหน้าตั้งค่าไม่เจอ
- **Effort คร่าวๆ:** Low (ผูก Event Click บน QStatusBar ไปเรียก Action เปิดหน้าต่าง Team)

## สรุปแนวทาง (Phased Execution)
- **Phase 1 (Quick Win):** แก้ไข Copy, ใส่ปุ่ม Redirect ใน HTML (Legacy) ยิงกลับมายัง PyQt (Team Window) เพื่อลดความซ้ำซ้อน และเพิ่มปุ่ม Quick Access ที่ Status Bar
- **Phase 2 (Migration):** ทยอยเขียน PyQt Widgets ใน Team Window สำหรับฟีเจอร์ที่เหลือใน Legacy (เช่น Pipeline Builder, Skill Matrix) เพื่อเตรียม Retire หน้าต่าง HTML เต็มรูปแบบในอนาคต
