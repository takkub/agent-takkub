# #253 — `takkub wait` ทำให้ Lead หูหนวก: interrupt-wake + timeout cap

## อาการ

Lead ติด foreground `takkub wait --role qa --timeout 3000` 9 นาที ระหว่างนั้น devops
รายงาน done เข้ามา — ข้อความ **ไม่ได้หาย** (ไปนั่งเป็น queued input / durable pending
store ตามดีไซน์ #163/#70 ของ `lead_inbox.py`) แต่ Lead ไม่ตอบสนองอะไรเลยจนกว่า `wait`
จะ resolve role ที่ระบุใน `--role` หรือครบ `--timeout`

Root cause สองจุด (พิสูจน์จากโค้ดก่อนแก้):
- `lead_inbox.py`'s notify pump เป็น ready-gated เต็มรูปแบบ — ไม่มีเส้นทางไหน
  บังคับให้ Lead "หยุดสิ่งที่ทำอยู่" มารับ notice
- `cli.cmd_wait` + `lead_wait.LeadWaitMixin.poll_wait` เดิม resolve เฉพาะ role ที่อยู่ใน
  `--role` เท่านั้น ไม่มีเงื่อนไขตื่นเมื่อ role อื่นมี notice ค้าง — `--timeout` สูงสุด
  (เดิม 7200s/2h) = Lead เงียบได้นานขนาดนั้นแม้มี FAILED report ของ role อื่นรอ

## แก้อะไรบ้าง

### 1. Interrupt-wake (`orchestrator.py` + `lead_wait.py` + `cli.py`)

`Orchestrator._pending_notice_outside(project_ns, watched_roles)` (ใหม่, ข้าง
`_has_pending_lead_notice`) สแกน `inbox_report()` เดิม (digest/live/durable 3 queue)
หา notice แรกที่:
- role ต้นทาง **ไม่อยู่ใน** `watched_roles` (role ของตัวเองไม่ interrupt ตัวเอง — ทางนั้น
  resolve ผ่าน `done`/`failed` ปกติอยู่แล้ว)
- ผ่าน `_is_blocking_lead_notice()` เดิม (FAILED / `[spawn-failed]` /
  `[delivery-unconfirmed]` / `[spawn-stuck]`) — **ไม่ใช่** plain `[role done]`

จงใจไม่ให้ plain done ของ role อื่น interrupt: fan-out พร้อมกันหลาย role เป็นเรื่องปกติ
มาก ถ้าตื่นทุกครั้งที่เพื่อน role ไหน done จะทำให้ `wait --role X` แทบไร้ประโยชน์
(กลายเป็น poll ทุกไม่กี่วินาทีอยู่ดี) — เฉพาะ report ที่ต้องการการตัดสินใจจริงๆ
เท่านั้นที่ควร jump คิว เหมือนที่ `_is_blocking_lead_notice` ใช้ jump digest queue อยู่แล้ว

`LeadWaitMixin.poll_wait` เรียก `_pending_notice_outside` ก็ต่อเมื่อยังมี role ที่ watch
อยู่ค้าง (`pending` ไม่ว่าง — ถ้า resolve หมดแล้วก็กำลังจะจบ registration เองอยู่แล้ว
ไม่ต้องเสียรอบสแกน) ผลลัพธ์ใส่ใน response ช่องใหม่ `"interrupt": {"role", "detail"} | None`
และ**จบ registration ทันทีเหมือน natural resolution** (role ที่ watch อยู่ยัง pending
จริงในเพนของมันเอง แค่ตัว wait tracking นี้จบ — Lead เรียก `takkub wait` ใหม่เพื่อ resume
watch ได้)

`cli.cmd_wait` เช็ค `poll["interrupt"]` ทุกรอบ poll — เจอแล้ว break loop ทันที (ไม่ sleep
ต่อ ไม่ poll ต่อ) พิมพ์ role/เนื้อหาที่ทำให้ตื่นชัดเจน + ชี้ทางต่อ (`takkub inbox` แล้ว
`takkub wait` ใหม่) ผลลัพธ์สุดท้ายถือเป็น **ไม่ success เต็ม** (`ok: False`, exit code 1)
เหมือนเคส timeout เดิม เพราะ role ที่ตั้งใจ watch ยังไม่ resolve จริง — แยก label ข้อความ
"wait interrupted, see above" ออกจาก "timeout reached" ให้ไม่สับสนกัน

### 2. Timeout cap (`cli.py`)

`_WAIT_MAX_TIMEOUT_S`: 7200s (2h) → **1800s (30 min)**, `_WAIT_DEFAULT_TIMEOUT_S` คงที่
1800s (เท่ากับ cap ใหม่พอดี) เหตุผล: interrupt-wake ปิดช่องโหว่หลักไปแล้ว (blocking
report จาก role นอก watch ไม่ต้องรอครบ timeout อีกต่อไป) แต่ plain-done ของ role อื่น
ยังไม่ interrupt โดยเจตนา — เคสที่ watch role ช้า 1 ตัวขณะ role เร็วอื่นๆ done เฉยๆ ก็ยัง
เงียบได้เท่าที่ timeout อนุญาตอยู่ดี เพดาน 2h เดิมเป็น foot-gun ซ้อนอีกชั้นที่ไม่จำเป็นแล้ว
ลดเหลือ 30 นาที (เท่า default เดิม) บังคับให้ task ที่ยาวเกินนั้นต้องถูก re-issue เป็น
checkpoint ระหว่างทาง แทนที่จะกลายเป็นการ park ยาวๆ แบบไม่มีจุดกลับมาคิด

## ไม่แตะ

- สัญญาเดิมของ #242/#249 ทั้งหมด: waiter เดียวต่อ project, attach ได้ (union role
  set), cancel ได้ (`--cancel`), gone/pending/done/failed per-role reasoning, resolved-echo
  สำหรับ straggling attacher — ไม่มีจุดไหนถูกแก้
- multi-provider: `_pending_notice_outside` อ่านจาก queue state (`_lead_digest_queue`
  / `_lead_notify_queue` / `_pending_done_notices`) ที่ orchestrator เก็บอยู่แล้วสำหรับ
  ทุก provider เหมือนกัน ไม่มีจุดไหนผูก claude
- cross-platform: ไม่มี path/OS-specific code ในทุกจุดที่แก้

## ทดสอบ

`tests/test_lead_wait.py` — เพิ่ม 5 เทสใหม่:
- `TestPollWaitInterrupt` (4 เทส): blocking notice นอก watch → interrupt; plain done
  นอก watch → ไม่ interrupt; blocking notice ของ role ที่ watch เอง → ไม่ self-interrupt;
  ไม่มี pending role เหลือ → ไม่เสียรอบสแกน `_pending_notice_outside` เลย
- `TestCliWaitCommand::test_interrupt_stops_the_loop_early_with_a_clear_reason`: CLI
  loop หยุดทันทีที่ poll แรกเจอ interrupt (ไม่ poll ต่อ), พิมพ์ role/เนื้อหาชัดเจน,
  `rc == 1` (เหมือน timeout, ไม่ใช่ full success)

รันผ่าน shared `.venv` + `PYTHONPATH=src` (ไม่ `pip install -e .` — #202) — 42/42 ผ่าน รวม
`tests/test_inbox_report.py` (20/20, ไฟล์ที่เกี่ยวเพราะ `_pending_notice_outside` reuse
`inbox_report()`) ยังเขียวเหมือนเดิม ไม่ได้รัน full suite (ตาม test-tier policy —
qa รันตอน batch gate)
