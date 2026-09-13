# รายงานที่ต้องแชร์ให้ user (#367) — takkub report publish

เมื่อ user ต้องการลิงก์รายงาน/dashboard ที่แชร์ต่อได้ **แต่ไม่ต้องการให้ผู้รับเห็น URL claude** (เช่น `claude.ai/code/artifact/...`) → ใช้ `takkub report publish <file.html> [--name n] [--project p] [--expires 30d] [--label "..."]` แทน Claude Artifact (Lead-only mutation) รายละเอียด flag ทั้งหมด + `list`/`revoke`/`rotate` → `docs/lead/cli-reference.md`

**ข้อจำกัด (บอก user ทุกครั้งที่ publish):** ลิงก์เปิดจากนอกเครื่องได้เฉพาะตอน Remote เปิดอยู่จริง (Settings → Remote enabled + tunnel connect) — คำสั่งนี้ไม่เปิด Remote ให้อัตโนมัติ, publish/list จะพิมพ์บรรทัดสถานะ Remote ให้เสมอ (เปิด/ปิดอยู่)
