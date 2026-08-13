# Auto-Failover Design Document

## 1. Trigger Threshold ("ใกล้จะหมด" วัดอย่างไร)
การวัดว่าโควต้าใกล้จะหมด (Threshold) เพื่อเริ่มทำ Auto-Failover จะพิจารณาจากทั้งสองปัจจัย (ผสมกัน):
- **Usage Percentage**: เช็คผ่าน `LimitStore` / `fetch_usage_shared()` หาก usage ทะลุ 95% ของโควต้าที่ตั้งไว้
- **Hard Error (429/Quota Exceeded)**: หาก API ตอบกลับมาว่า Rate limit หรือ Quota exceeded (ชน limit ทันทีโดยที่ percentage อาจจะยังตามไม่ทัน) ให้ถือเป็น trigger ทันที

## 2. Provider Selection Order (การเลือก Provider สำรอง)
- **Priority List**: ควรกำหนด Fallback Priority List สำหรับแต่ละ Role (เช่น Claude -> Gemini -> OpenAI)
- **Quota Validation**: ก่อนจะเลือก Provider สำรอง ระบบต้องตรวจสอบผ่าน `fetch_usage_shared()` ของ Provider นั้นๆ ว่ามีโควต้าเหลือเพียงพอ (เช่น < 80% usage) เพื่อป้องกันการ Failover ไปหา Provider ที่กำลังจะชน limit เช่นกัน
- การเลือกใช้กลไก `effective_provider_for` ควรอัปเดตให้รองรับการเช็ค quota ไม่ใช่แค่สถานะ on/off

## 3. Handoff Artifact (ข้อมูลสำหรับส่งมอบงาน)
เพื่อไม่ให้งานสะดุด ตัวใหม่ที่มารับช่วงต่อต้องได้รับ Context สรุปงาน โดยระบบจะสร้างไฟล์ `handoff_state.json` (หรือ Markdown) อัตโนมัติ ประกอบด้วย:
- `original_task`: คำสั่งเริ่มต้นของงาน
- `completed_steps`: สรุปสิ่งที่ทำเสร็จไปแล้ว (สกัดจาก history/transcript ล่าสุด)
- `modified_files`: รายชื่อไฟล์ที่มีการแก้ไขไปแล้วใน session นี้
- `current_working_directory`: Path ปัจจุบัน
- `last_stdout/stderr`: Output สุดท้ายก่อนเกิดการ Failover

## 4. Role-Specific Handling (ครอบคลุมทุก Role รวมถึง Lead)
- **Teammate**: ใช้ Handoff Artifact ทั่วไปตามข้อ 3 ส่งเข้าไปใน System Prompt หรือ Message แรกของ Provider ใหม่
- **Lead**: เนื่องจาก Lead มี Context พิเศษ การ Failover ต้องดึงค่า `BLOCKED_DIRS`, Session Brief, และ State ของทีมที่กำลังทำงานอยู่ (Active panes) ไปด้วย เพื่อให้ Lead ตัวใหม่สามารถเชื่อมต่อกับ Teammate ที่กำลังทำงานอยู่ได้โดยไม่เสีย Context การควบคุม

## 5. Cross-Platform Compatibility (Windows vs macOS)
- กระบวนการ Failover จะต้องจัดการกับการปิดและเปิด Process ใหม่ให้เข้ากับ Platform:
  - **Windows (ConPTY)**: ทำการส่งสัญญาณปิด process อย่างถูกต้องเพื่อไม่ให้เกิด Zombie process
  - **macOS (_pty_backend)**: ทำการ cleanup PTY file descriptors ให้เรียบร้อย
- โค้ดส่วน Failover จะต้องไม่ผูกติดกับ OS-specific signals แต่ควรใช้ abstraction ของ PTY lifecycle ที่มีอยู่ในระบบ

## 6. Dev/Prod Isolation
- การเช็คและการตั้งค่าสถานะโควต้าและ Failover ใน `LimitStore` จะต้องมีการทำ Namespace หรือใช้ Key ที่ผูกกับ Environment (เช่น Prefix `dev:` หรือ `prod:`) หรือใช้ Path ของ Workspace เป็นตัวแยก เพื่อป้องกันไม่ให้ Dev instance ไปแย่งโควต้าหรือ Trigger failover ของ Prod instance

## 7. Fallback (กรณีไม่มี Provider อื่นว่างเลย)
หากทุก Provider ชน Limit หรือใช้งานไม่ได้ (อ้างอิงจาก Issue #158 ที่เงียบหายและเสียงาน) ระบบต้อง:
1. **Park the State**: บันทึก Handoff Artifact และ State ทั้งหมดลงดิสก์
2. **Alert**: แจ้งเตือนอย่างชัดเจนผ่าน UI/Console (เช่น "CRITICAL: All providers exhausted. Task parked.")
3. **Auto-Wakeup Cron**: ตั้งเวลา (เช่น ทุกๆ 15 นาที) เพื่อตรวจสอบผ่าน `fetch_usage_shared()` ว่ามี Provider ใดโควต้า Reset แล้วหรือไม่ หากมี ให้ทำการ Auto-resume จาก Handoff Artifact ทันที
