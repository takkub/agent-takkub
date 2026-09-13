# Unavailable providers → Claude substitute

codex/gemini ใช้ไม่ได้ (toggle ปิดใน Settings หรือ CLI ไม่ได้ติดตั้ง) → **ไม่ต้อง refuse** — orchestrator degrade เป็น claude อัตโนมัติ (pane ชื่อ role เดิม อ่าน stand-in role file รายงาน `[claude-substitute for <role>]`) · Lead แค่**บอก user 1 บรรทัด** ว่า "X ใช้ไม่ได้ → Claude รับแทน (เสีย model diversity)" ไม่ต้องหยุดรอ · งานที่ต้องการ cross-check ต่างโมเดลจริงๆ substitute ไม่ได้ประโยชน์นั้น — flag ให้ user รู้
