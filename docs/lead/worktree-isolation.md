# Worktree & Parallel Dispatch (#81, #408, #544)

**Default parallel ไว้ก่อน** — task ไม่ depend output กัน → ส่งคู่ขนาน (`&` + `wait`) อย่ารอ done ทีละตัว
**Decision rule:** task A ใช้ output จาก task B ไหม? ใช่ = sequential · ไม่ใช่ = parallel (`routing_planner.classify()` เช็ค dependency signal ให้แล้ว — "ตาม schema"/"ใช้ข้อมูลจาก endpoint" → บังคับ sequence)

**Execution mode** (always PARALLEL / Multi mode):
- Request มีหลาย feature อิสระ → แตกเป็น K features → fan out `role#1..#K` พร้อมกัน · **หลาย instance แก้ repo เดียวกัน → `--isolation worktree` ทุกตัว** (#81) — done → merge proposal, Lead review diff + merge ทีละอัน · งานจำนวนมากจัดเป็น waves กันเครื่องค้าง · งาน depend กันยัง sequential

**กฎ verify flow (#585):**
- **scope=tiny:** **ห้ามเรียก QA/reviewer** — Lead อ่าน diff เองแล้วจบภารกิจได้เลย ไม่ต้องมี verify chain
- **scope=normal/deep:** **QA = ปุ่มจบ รันท้ายสุดเสมอ** ต่อเมื่อ (1) DEV เสร็จหมดทุกอย่าง (2) โปรเจคมี docker compose → devops ยก stack port-safe ก่อน · ไม่มี compose → ตรงไป QA (สำหรับ deep: รัน `takkub qa-gate --auto` ท้าย batch; tiny/normal ไม่ต้องรัน qa-gate) · reviewer = ตอน PR (ไม่อยู่ใน auto gate ยกเว้น trust-boundary/schema/migration) · DEV ยังไม่จบ = **ห้ามเรียก QA**

ตัวอย่างเต็มทุก pattern (parallel/sequential/ผสม/auto-chain/shards/plan-first/goal/critic pipeline) → **`docs/lead/patterns.md`**
