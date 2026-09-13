# Effort routing (#323) & Tiered scanning

## Tiered scanning
งาน audit/scan รอบแรกของ qa/reviewer/critic ให้ `--model <haiku-or-flash-id>` แล้ว escalate เมื่อเจอประเด็นยาก + ตอน final gate · `--model` มีผลเฉพาะ pane spawn ใหม่ (เปิดอยู่ = `takkub close --role` ก่อน)

## Effort routing (#323)
`takkub assign --effort low|medium|high` — one-assign override เหมือน `--model`/`--provider`, มีผลเฉพาะ pane spawn ใหม่, default = ไม่ส่ง (ใช้ effort เดิมของ role/provider) รองรับจริงวันนี้ 3 provider: **claude** (`--effort`), **codex** (`-c model_reasoning_effort=`), **agy/gemini** (`--effort`, fix ต้นทาง #125 ใน agy 1.1.10 — เดิมเคย disable เพราะ model/effort ชนกันแล้ว agy swap model เงียบ ตอนนี้ agy เอง hard-error ชัดแทน) — ทั้งสามรับ low/medium/high ตรงตัว; provider ที่เหลือ (opencode/kimi/cursor) ยังไม่มี CLI knob (gap #103) → assign ไม่ error แต่ effort ถูก drop เงียบ (documented degrade) — เช็ค gap ก่อนพึ่ง effort เป็นตัวตัดสินความเร็ว/คุณภาพงานถ้า role นั้น map ไป provider ที่ไม่รองรับ
- **low** → งาน mechanical ไม่ต้องใช้วิจารณญาณ: rename, doc/CLAUDE.md sync, รัน test suite ที่มีอยู่แล้ว, one-line config bump — เร็ว+ถูก ตรง north star (throughput)
- **medium** → default ของ role ส่วนใหญ่อยู่แล้ว (ดู `_teammate_tier` ใน orchestrator_text.py) ไม่ต้องระบุถ้าไม่มีเหตุผลชัดจะ override
- **high** → งานที่พลาดแล้วแพง: schema/migration, auth/security, refactor ข้าม module, หรือ role gate ที่เป็นด่านสุดท้ายก่อน merge (reviewer/critic final pass) — ปกติ role tier default (`orchestrator_text._ROLE_MODEL_TIERS`) ตั้ง high ให้อยู่แล้วสำหรับ role เหล่านี้ ใช้ `--effort` เมื่อ**เบี่ยงจาก default ของ role นั้นชั่วคราว**เท่านั้น ไม่ใช่ตั้งถาวร (ถาวร → Settings → Providers & Roles)
