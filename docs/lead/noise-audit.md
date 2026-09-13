# Lead noise audit (#464)

ตอนรัน `takkub ma` (หรือก่อน end-session) อ่าน section **"Lead noise (24h)"** เสมอ — นับ `lead_notice` ทุก kind ที่ `_notify_lead` ยิงเข้า Lead จริง พร้อม emitter (`ไฟล์:บรรทัด`) และ ครั้ง/ชม. kind ที่ขึ้น **`⚠ บ่อย`** (> `TAKKUB_NOISE_PER_HOUR`, default 4/ชม.) ให้**เปิดใบทันทีด้วยคำสั่งที่ report พิมพ์ไว้ให้แล้ว** (`takkub issue "notice <kind> ที่ <emitter> บอกบ่อย …"`) — ชี้ emitter ตรงๆ **ห้ามแค่บ่นเฉยๆ** ถ้าเลือกเปิดใบ ต้องใส่เหตุผลจริงแทน placeholder `<ระบุเหตุผล>`

ข้อความที่บอกตัวเองว่า **"ปลอดภัย ไม่ต้องทำอะไร"** คือสัญญาณว่า notice นั้นไม่ควรส่งถึง Lead ตั้งแต่แรก (เทียบ #464 item 1) — เจอแบบนี้ = เปิดใบเสนอเอาออกจากช่องทาง Lead ไปเป็น audit log อย่างเดียว
