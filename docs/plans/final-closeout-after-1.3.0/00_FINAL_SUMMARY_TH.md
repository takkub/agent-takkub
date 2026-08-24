# สรุปสุดท้าย

หลัง 1.3.0 ระบบหลักถือว่า "ครบแล้ว" เหลือ 5 เรื่องที่ควรปิดให้จบ:

1. #376 provider delivery bug — ถ้ายังไม่ปิด ให้ปิดและทดสอบจริงก่อน
2. OpenViking strict project isolation — ห้าม Project A ดึงข้อมูล Project B
3. Settings UI + Context Debug — ลดการใช้ env/CLI และมองเห็น context/token
4. Real validation — ลอง 21st/Figma/Penpot/OpenViking + GUI จริง
5. Token/Context gating — งานเล็กห้ามเรียกทุก source/tool โดยไม่จำเป็น

สิ่งที่ไม่ควรทำตอนนี้:
- ไม่รื้อ Brain
- ไม่รื้อ Conversation
- ไม่แทน Graft
- ไม่ทำ OpenViking เป็น source-of-truth operational memory
- ไม่แตะ Phase 10/#362 ปนกับงานนี้
- ไม่เพิ่ม local LLM dependency

หลังปิดชุดนี้ ให้ "หยุดเพิ่ม architecture" และเข้าสู่ช่วงใช้งานจริง/soak/เก็บ bug จาก field usage
