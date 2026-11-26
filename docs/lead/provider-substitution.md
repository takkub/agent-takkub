# Unavailable providers → Claude substitute

codex/gemini ใช้ไม่ได้ (toggle ปิดใน Settings หรือ CLI ไม่ได้ติดตั้ง) → **ไม่ต้อง refuse** — orchestrator degrade เป็น claude อัตโนมัติ (pane ชื่อ role เดิม อ่าน stand-in role file รายงาน `[claude-substitute for <role>]`) · Lead แค่**บอก user 1 บรรทัด** ว่า "X ใช้ไม่ได้ → Claude รับแทน (เสีย model diversity)" ไม่ต้องหยุดรอ · งานที่ต้องการ cross-check ต่างโมเดลจริงๆ substitute ไม่ได้ประโยชน์นั้น — flag ให้ user รู้

## slot ทดแทนไม่ใช่มือเพิ่ม (#1058)

ใช้ slot ของ provider ที่ปิดเฉพาะเมื่อ user ขอ role นั้นเอง (เช่น cross-check) — **ห้าม**ย้ายงานของ frontend/backend ไป `cursor`/`opencode`/`mobile` เพราะ pane เจ้าของงานไม่ว่าง: user ปิด provider นั้นไว้ (เห็นชื่อมันทำงาน = เข้าใจว่าเป็นบั๊ก) และ pane ทดแทนใช้กติกาของ slot จึงไม่ self-verify งาน UI

role ที่ต้องใช้ไม่ว่าง:
- ไฟล์ชุดเดียวกับงานที่ทำอยู่ → `takkub send --to <role>` ต่อท้าย
- คนละไฟล์ → assign role เดิมอีกตัวด้วย `--isolation worktree`
- ไม่งั้น assign role เดิม — เข้าคิวและส่งเองเมื่อ pane ว่าง

คำเตือนการสลับ provider แจ้งครั้งเดียวต่อคู่ provider ต่อ session (ครั้งถัดไปลง events.log อย่างเดียว)
