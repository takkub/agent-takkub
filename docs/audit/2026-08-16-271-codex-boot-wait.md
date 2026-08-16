# #271 — codex `ready_wait_ms` สั้นกว่าเวลาบูตจริง → blind paste งานตกหล่น

**สถานะ:** แก้แล้ว (ข้อ 1-2), targeted tests เขียว · ข้อ 3 (`--disable code_mode`) **ไม่แก้** ตามที่ issue สั่งชัดเจน (ต้องวัดก่อน ห้ามใส่เดา)

## อาการ (จาก issue, วัดจริงบนเครื่อง user)

- `provider_spec.py` เดิม: `codex_spec.ready_wait_ms = 90_000`
- เวลาบูตจริงของ codex บนเครื่องนี้ 90-150 วินาที **ทุกรอบ** (spawn 4 รอบวันเดียว 2026-08-16, ทั้ง 4 รอบเกิน 90s, `[delivery-boot-stall]` notice #254 ยิงที่ 110s ทุกรอบ)
- ผล: `ready_wait` หมดอายุระหว่างยังบูตอยู่ → ระบบ paste แบบ blind (#26) เข้า composer ที่ยังไม่พร้อม → งานตกหล่น 3/4 รอบ ต้องให้ Lead ส่งซ้ำเอง

สาเหตุที่ codex บูตช้าลง: feature ใหม่ `code_mode`/`codex_apps` (child process `codex-code-mode-host.exe`, จอขึ้น "Booting MCP server: codex_apps (0s - esc to interrupt)") — **ไม่ใช่ MCP ที่ cockpit ตั้งเอง** (ยืนยันด้วย `codex mcp list` = ว่างเปล่า) ผูกกับ codex 0.147.0 เอง `ready_wait_ms=90_000` ถูกตั้งไว้ตั้งแต่ก่อน codex มี feature นี้

## Fix 1 (หลัก) — ห้าม blind-paste ขณะยังเห็น boot marker

`lead_inbox.py::_send_when_ready`'s `_check()`: จุดตัดสินใจ blind-paste เดิม (`elapsed[0] >= max_wait_ms` → เช็ค `seconds_since_output()` เทียบ `STALL_THRESHOLD_SEC`/`BUSY_WAIT_CEILING_SEC` แล้ว `_deliver(unconfirmed=True)`) **ไม่เคยเช็ค `shows_startup_marker()` เลย** — แก้โดยเพิ่ม guard ก่อนถึงจุดนั้น:

- ถ้า `pane.session.shows_startup_marker()` ยังเป็น True ณ ตอน `elapsed >= max_wait_ms` → **ไม่ blind-paste** แต่ยืดการรอต่อ (reschedule เหมือน busy-wait branch เดิม)
- เพดาน: ผูกกับ `BUSY_WAIT_CEILING_SEC` เดิม (1800s default) — บูตที่ไม่มีวันจบจริงๆ ยังได้ blind-paste แบบ last-resort ไม่ค้างตลอดไป (log event ใหม่ `task_deliver_boot_marker_ceiling_timeout`)
- แจ้ง Lead: ใช้กลไก `[delivery-boot-stall]` เดิมของ #254 (ยิงที่ `BOOT_STALL_GRACE_SEC`=110s อยู่แล้ว จากการเช็ค marker ตัวเดียวกัน) — ไม่ต้องเพิ่ม notice ใหม่

**Refactor ที่มากับ fix นี้:** เดิม `shows_startup_marker()` ถูกเรียกเฉพาะใน `if not boot_stall_warned[0]:` block (เพื่อ accumulate `boot_stall_elapsed`) — ครั้งเดียวหลัง warn แล้วจะไม่ถูกเรียกอีกเลย ทำให้ guard ใหม่ (ต้องรู้สถานะ marker ทุก poll ไม่ว่าจะ warn แล้วหรือยัง) เรียกซ้ำไม่ได้ตรงๆ (จะ desync กับ test double ที่ใช้ finite `side_effect` list — เจอจริงตอนรันเทส `test_delivery_boot_stall_notice.py` พังเพราะเรียกสองครั้งต่อ poll) → ย้ายการเรียก `shows_startup_marker()` ขึ้นมาที่จุดเดียวต้นๆ ของ `_check()` (คำนวณทุก poll ไม่มีเงื่อนไข) เก็บเป็น `_still_booting` แล้วให้ทั้ง boot-stall accumulator (บรรทัดเดิม) และ blind-paste guard (บรรทัดใหม่) ใช้ค่าเดียวกัน — เรียก session method แค่ครั้งเดียวต่อ poll เหมือนเดิม

## Fix 2 — ยก `ready_wait_ms` ของ codex

`provider_spec.py`: `codex_spec.ready_wait_ms` 90_000 → **180_000** พร้อมคอมเมนต์อ้างตัวเลขที่วัดได้ (90-150s, 4/4 trials) — ครอบการกระจายจริงด้วย margin คู่กับ fix 1 (fix 1 คือ backstop จริง fix 2 แค่ลดการพึ่งพา extension ในเคสปกติ)

## Fix 3 — `--disable code_mode`: **ไม่ทำ**

ตาม issue สั่งชัดเจนว่าห้ามใส่จนกว่าจะวัดได้ว่า (ก) ตัดเวลาบูตได้เท่าไหร่จริง (ข) เสียความสามารถอะไรบ้าง — ไม่มีการวัดในรอบนี้ จึงข้ามข้อนี้ทั้งหมด ไม่แตะ `autonomy_flags` ของ codex

## Tests

ไฟล์ใหม่ `tests/test_boot_marker_blind_paste_guard.py` (4 เทส):
- marker ค้างเกิน `max_wait_ms` นาน (20 poll จำลอง) → ต้องไม่มี blind-paste (`[delivery-unconfirmed]`) จนกว่า marker หายและ pane ready จริง ถึงส่งงานแบบปกติ (ไม่ unconfirmed)
- marker ไม่หายเลย (ceiling `BUSY_WAIT_CEILING_SEC` เข้ามาแทน) → ยังส่งงานแบบ last-resort (unconfirmed) ไม่ค้างตลอดไป
- pane ready ทันที → ส่งงานทันทีเหมือนเดิม (ไม่ regress path ปกติ)
- pane ค้างจริงแบบไม่มี boot marker เลย → ยัง blind-paste ตาม #26 เดิม (guard นี้ scope เฉพาะ boot-marker case เท่านั้น ไม่ได้ผ่อน stall tolerance ทั่วไป)

อัปเดตเทสเดิมที่ผูกกับค่าคงที่: `test_delivery_unconfirmed.py::TestReadyWaitMs::test_codex_gets_longer_window_on_default` (90_000 → 180_000, การเปลี่ยนแปลงที่ตั้งใจจาก fix 2)

รันแล้วเขียวทั้งหมด (targeted, ผ่าน `.venv`/`PYTHONPATH=src`, ไม่รัน full suite ตาม policy):
- `test_boot_marker_blind_paste_guard.py`
- `test_delivery_boot_stall_notice.py` (regression guard #254 — เขียวหลัง refactor เรียก marker ครั้งเดียว)
- `test_delivery_busy_wait_notice.py` (regression guard #144/#130/#131)
- `test_delivery_unconfirmed.py` / `test_delivery_unconfirmed_status_flag.py` (regression guard #26)
- `test_lead_wait.py`, `test_delivery_supersede.py` (#235), `test_orchestrator_stall.py`, `test_auto_trust_wait_window.py`
- ทุก provider/codex-tagged test (`-k "provider_spec or codex"`)

## Multi-provider

Guard ใหม่อ่านผ่าน `PtySession.shows_startup_marker()` ซึ่ง provider-agnostic อยู่แล้ว (`_STARTUP_MARKERS` ครอบทั้ง codex และ agy — pty_session.py) — ใช้ได้กับทุก provider ที่มี startup marker โดยไม่ต้องเพิ่มโค้ดเฉพาะ codex เลย (gemini ที่ค้าง sign-in/boot เข้าเคสเดียวกันอัตโนมัติ)

## ไฟล์ที่แก้

- `src/agent_takkub/lead_inbox.py` — blind-paste guard + refactor เรียก `shows_startup_marker()` ครั้งเดียวต่อ poll
- `src/agent_takkub/provider_spec.py` — `codex_spec.ready_wait_ms` 90_000 → 180_000
- `tests/test_boot_marker_blind_paste_guard.py` — ใหม่
- `tests/test_delivery_unconfirmed.py` — อัปเดตค่าคาดหวัง
