# New Role View - Design Critique (2026-08-13)

จากการรันเรนเดอร์ UI จริงแบบ offscreen และเลื่อน ScrollArea จนสุด รวมถึงจำลองข้อมูล Skill ในระบบ พบประเด็นดังนี้:

## 1. ยืนยันด้วยหลักฐาน (Verified with evidence)

- **การ์ด Instructions และปุ่ม Create Role (แก้ไขข้อสรุปเดิม):** 
  จากการตรวจสอบโดยเลื่อน Vertical Scrollbar ลงไปจนสุด (ภาพ `new_role_bottom.png` และการแคปเจอร์ทั้ง widget ใน `new_role_full_widget.png`) **พบว่ามีการ์ด 5/5 Instructions และปุ่ม "+ Create Role" ปรากฏอยู่ครบถ้วน** และสามารถใช้งานได้ปกติ ข้อสรุปเดิมที่บอกว่าถูกตัดขาดนั้นผิดพลาดเนื่องจากรอบก่อนหน้าไม่ได้ทำการ Scroll ลงมา
- **การแสดงผล Skill List และ Counter (แก้ไขข้อสรุปเดิม):** 
  เมื่อทำการจำลอง (seed) ไฟล์ Skill เข้าไปใน `.claude/skills/` พบว่า **รายการ Skill แสดงขึ้นมาตามปกติ และ Counter (0/5 skill เลือก) สามารถทำงานได้จริง** 
  (หลักฐานจาก Widget state: พบ 5 skill ถูกเรนเดอร์ใน QCheckBox และความสูงของ container ด้านในคือ 324px ในขณะที่ max-height ของ ScrollArea คือ 220px ทำให้เกิด scrollbar ได้ถูกต้อง)
- **Horizontal Overflow (แถบเลื่อนแนวนอน):**
  จากการจำลองย่อหน้าจอเหลือความกว้าง 1000px (`new_role_narrow_bottom.png`) และเลื่อนลงมาด้านล่างสุด ไม่พบแถบ Scrollbar แนวนอนแต่อย่างใด ปัญหานี้ได้รับการแก้ไขอย่างสมบูรณ์แล้ว
- **Contrast ของ Placeholder Text:**
  ข้อความ Placeholder ใน QLineEdit/QPlainTextEdit ถูกเรนเดอร์ให้กลืนไปกับสีพื้นหลังจริง เนื่องจากไม่ได้มีการกำหนดสีเฉพาะสำหรับ `placeholder` ใน `cockpit_theme.py` (ระบบจึงใช้ default Qt ที่เป็นสี text เจือจาง) เมื่อพิจารณาจากสีพื้นหลัง `GROUND_INPUT` (`#1c1f26`) สีจึงมืดเกินไป
  *ข้อเสนอแนะ:* ควรกำหนดสี `::placeholder` ใน Stylesheet อย่างชัดเจน โดยแนะนำให้ใช้ token `TEXT_MUTED` (`#7b828f`) เพื่อให้ contrast ดีขึ้น

## 2. ตรวจไม่ได้ (Cannot verify)

- **การทำงานของปุ่ม "+ Create Role" และ Interaction สวิทช์:**
  เนื่องจากการทดสอบครั้งนี้เป็นการ Render State ในภาพนิ่ง ไม่สามารถยืนยันผลลัพธ์ Action ตอนกดปุ่ม (เช่น validation เมื่อกรอกชื่อผิด หรือการเซฟไฟล์จริง) ได้

## ภาพอ้างอิง (Rendered Screenshots)
ภาพทั้งหมดถูกบันทึกไว้ที่: `runtime/exports/2026-08-13/agent-takkub/screenshots/` (ทุกภาพมี MD5 ไม่ซ้ำกัน ยืนยันว่าเป็นการเก็บภาพใน State ที่ต่างกันจริง)
- `new_role_top.png` (MD5: `6cfc3b7bee82229335dc183647554fcd`) - ภาพหน้าต่างด้านบน
- `new_role_bottom.png` (MD5: `1c4534e62b443c9dfda4f24b1e9ee47d`) - ภาพหน้าต่างเมื่อเลื่อน Scroll ลงล่างสุด
- `new_role_full_widget.png` (MD5: `eeac9e95d9b4d4435e8c70b8f20a5da9`) - ภาพเนื้อหาทั้งหมดแบบไม่ตัด (Full height)
- `new_role_narrow_bottom.png` (MD5: `4d738c553ec72675939ee91da8fe0e0e`) - ภาพหน้าต่างแคบ 1000px ตอนเลื่อนลงล่างสุด
