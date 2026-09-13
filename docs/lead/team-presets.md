# Team preset (#512) — รายละเอียดและข้อบังคับ

**ขนาดทีมต่อโปรเจค** (+override ต่องาน) — งานเล็ก/bug scope ชัดไม่ต้อง boot ทั้งทีม ตั้งได้ที่ Settings → Providers & Roles หรือ `takkub team set <preset>`:

| preset | roster ที่เปิด | checker (qa/reviewer) | Lead แก้โค้ดเอง? |
|---|---|---|---|
| **ทำเอง** (`solo-lead`) | ไม่มี position เปิดเลย | ไม่มี | ได้ — ทดสอบเอง (targeted/screenshot) แล้วรายงาน **ห้าม spawn dev role ใดๆ** (frontend/backend/mobile/devops/qa/reviewer) — provider pane/`shell`/`critic` ยังเปิดตามปกติ (ดูย่อหน้าถัดไป) |
| **คู่** (`pair`) | ไม่มี position เปิดเลย | reviewer | ได้ — แก้เองแล้วสั่ง `takkub assign --role reviewer` ให้ตรวจอย่างเดียว |
| **ทีมเต็ม** (`full`) | frontend/backend/mobile/devops | qa (ปิดท้ายเหมือนเดิม) | **ไม่ได้** — มอบหมายผ่าน `takkub assign` ตามปกติ |
| **custom** | เลือกเอง (Settings) | เลือกเอง | ตามที่ตั้ง |
| **อัตโนมัติ** (`auto`, ค่าเริ่มต้น) | — ไม่ fix roster | — | Lead เสนอขนาดเองต่องานจาก `routing_planner.suggest_team_size()` แล้วพิมพ์เหตุผล 1 บรรทัดก่อนเริ่ม (ไม่ enforce อะไร) — เรียกได้จริงผ่าน `takkub team suggest "<task>"` (#510/#512 M2) |

Roster ของ preset ครอบเฉพาะ**ตำแหน่ง** frontend/backend/mobile/devops (+custom role ของโปรเจค) และ checker slot (reviewer/qa) — **provider pane (codex/gemini/opencode/kimi/cursor) กับ `shell`/`critic` ไม่ถูก preset แตะเลย เจตนา** ("ทำเอง" ก็ยัง `takkub assign --role codex`/`shell`/`critic` ได้ตามปกติเสมอไม่ว่า preset จะเป็นอะไร)

**เปลี่ยน preset (2026-09-07 M3 — อ่านก่อนใช้):** `takkub assign --role lead --team <preset> "task"` (per-task override) **ถูกปิดแล้วเมื่อสั่งจาก Lead pane เอง** — คำสั่งนั้นเข้าเงื่อนไข lead-only เดียวกับ `assign` ทุกตัว หมายความว่ามีแต่ Lead เท่านั้นที่เคยเรียกมันสำเร็จได้จริง = Lead ยกสิทธิ์แก้โค้ดให้ตัวเองแบบไม่มีใครเห็น (`lead_may_implement` ของ solo-lead/pair) — ตอนนี้ reject เสมอพร้อมบอกให้ใช้ทางอื่น ใช้ **`takkub team set <preset>`** แทน (ไม่ lead-only, เปลี่ยน standing preset ของโปรเจคแทนที่จะเป็น override เฉพาะงาน) หรือให้ user ตั้งจาก Settings/มือถือ — ไม่ว่าทางไหนก็ตาม **มีผลตอน Lead spawn รอบหน้าเท่านั้น** (M7 — `render_lead_settings` อ่านตอน spawn ไม่ใช่ live) ถ้า Lead รันอยู่ ให้บอก user ตรงๆ ว่าต้อง restart Lead ก่อนถึงจะ apply จริง อย่าเงียบ

**Enforcement จริง (ไม่ใช่แค่ prompt):** `takkub assign --role <X>` ที่ preset ไม่เปิด roster ให้ถูก **reject ทันที** (choke point เดียวกับ #510's rolesEnabled — `orchestrator.assign()` + `cli_server` sync pre-check + `pipeline_executor` hop-skip) — อย่าพยายาม spawn role ที่ preset ปิดไว้ ("ทำเอง"/"คู่" spawn dev role ไม่ได้เลยแม้จะสั่งตรงๆ)

**สถานะเปลี่ยนระหว่าง session:** cockpit inject `[system] team preset ...` message เข้า pane ทันทีเมื่อมีคน set/override/clear จาก Settings หรือ CLI — อ่านแล้วปรับแผนตาม ไม่ต้องถามยืนยันซ้ำ
