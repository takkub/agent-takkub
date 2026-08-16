# #254/#255 — task-delivery lifecycle: boot-stall escalation + delivery cancel/reap

สอง issue คนละ subsystem ตามที่ #255 ระบุไว้เอง แต่แก้พร้อมกันเพราะทั้งคู่โผล่มาจาก
incident เดียวกัน (assign codex 08:00:57 → pane ค้าง boot → delivery ค้างคิว →
Lead กู้เองด้วย `takkub send` → delivery เดิมยังไม่ถูกยกเลิก)

## #254 — pane ค้างเฟส boot แล้วเงียบยาว

### Root cause (พิสูจน์จากโค้ด)

`lead_inbox.py::_send_when_ready`'s `_check()` poll loop มี 2 เฟส:

1. **นอก `_deliver()`** — poll `is_at_ready_prompt()` จนกว่าจะ ready หรือ
   `max_wait_ms` หมด แล้วเข้า busy/stall split: `seconds_since_output() <
   STALL_THRESHOLD_SEC` → ถือว่า "ยังไม่นิ่ง" → ยืดรอต่อจนครบ `BUSY_WAIT_CEILING_SEC`
   (1800s) โดยแจ้ง Lead **ครั้งเดียว** ตอนเริ่มยืด (#144, `_warn_lead_delivery_busy_wait`)
2. **ใน `_deliver()`** — self-heal resend chain (`_delayed_enter_verified`) ที่มี budget
   จำกัดของตัวเอง (~90s)

incident จริง: `_deliver()` **ไม่เคยถูกเรียกเลย** (composer ยัง placeholder) — แปลว่า
ติดอยู่ในเฟส 1 ตลอด เหตุผลคือ codex TUI spinner ระหว่าง "Booting MCP server: codex_apps
(0s • esc to interrupt)" redraw ต่อเนื่อง → `seconds_since_output()` ไม่โตพ้น
`STALL_THRESHOLD_SEC` เลย → เข้าเงื่อนไข "busy ไม่ใช่ stuck" ตลอดไป → ยืด extension
ซ้ำๆ เงียบๆ จนครบ 1800s โดยมีแค่ notice ใบเดียวตอน ~91s (ตรงกับ transcript จริง)

### ที่แก้

**ไม่ได้เพิ่ม provider-marker table ใหม่** — เจอ `PtySession.shows_startup_marker()` +
`_STARTUP_MARKERS` (`"booting mcp server"`, `"starting mcp server"`, `"tab to queue
message"`) ที่มีอยู่แล้วใน `pty_session.py` (ใช้โดย idle watchdog กันเรื่อง "forgot
`takkub done`" ระหว่าง boot) — global, ไม่ผูก provider ใดๆ ตรงเงื่อนไข multi-provider
ตรงตัวกว่าการเพิ่ม `ProviderSpec.boot_markers` เฉพาะ codex (ลองร่างไว้ก่อนแล้ว revert
ทิ้งตอนเจอตัวนี้ — ดู git history ของ commit นี้)

`lead_inbox.py::_check()` เพิ่ม closure-local accumulator (`boot_stall_elapsed`,
รูปแบบเดียวกับ `prompt_defer_elapsed` ของ #186) เช็คทุก poll:
- `shows_startup_marker()` เป็น True ติดต่อกัน → บวกสะสม `_READY_POLL_INTERVAL_MS`
- เป็น False → reset เป็น 0 (การ redraw ไม่ต่อเนื่องไม่ควรถูกนับสะสมข้าม stall คนละครั้ง)
- สะสมถึง `BOOT_STALL_GRACE_SEC` (ใหม่, `orchestrator.py`, default **110s**, env
  `TAKKUB_BOOT_STALL_GRACE_SEC`) → เรียก `_warn_lead_delivery_boot_stall()` **ครั้งเดียว**
  ต่อ delivery (gate ด้วย `boot_stall_warned[0]`)

**ทำไม 110s:** สูงกว่า `ready_wait_ms` ที่ codex/gemini/opencode/kimi/cursor ใช้เป็น
cold-boot allowance ปกติอยู่แล้ว (90s, `provider_spec.py`) พอที่ boot ปกติจะไม่ trip
เขตนี้ผิดๆ แต่ต่ำกว่า `BUSY_WAIT_CEILING_SEC` (1800s) มาก — Lead ได้ยินใน ~2 นาที
แทนที่จะเป็น 30 นาที (อยู่ในช่วง 90-120s ที่ user เสนอ)

Notice ใหม่ `[delivery-boot-stall]` **แยกจาก** `[delivery-busy-wait]` (#144) โดยตั้งใจ —
เพิ่มเข้า `_BLOCKING_NOTICE_MARKERS` ให้ jump digest queue เหมือน `[spawn-failed]`/
`[delivery-unconfirmed]`/`[spawn-stuck]` เพราะเป็นสัญญาณ "task ยังไม่ถึงมือ" ระดับเดียวกัน
เนื้อความบอกชัดว่าเป็น boot phase (ไม่ใช่ generic busy) + escape hatch ที่ทำได้จริงตอนนี้
(`takkub close --role <r>` แล้ว assign ใหม่ — task เดิมยังดูได้ผ่าน `takkub task show`)
ไม่ได้เพิ่ม one-command auto-restart primitive (นอก scope ของรอบนี้ — ยังต้อง close+assign
เอง แต่ Lead รู้ตัวเร็วกว่าเดิมมากพอที่จะลงมือ)

### Regression guard ที่ต้องมี

`_check()` เดิมเรียก `session.is_at_trust_prompt()` / `is_blocked_on_tty_prompt()` /
`is_blocked_on_permission_prompt()` ซึ่ง test fixture ทุกไฟล์ pin ค่า return ไว้แล้ว
(unconfigured `MagicMock()` return ค่า truthy Mock โดย default) — `shows_startup_marker()`
เป็น call **ใหม่** ที่ไม่มี fixture ไหน pin ไว้ ถ้าไม่กัน จะทำให้ทุก test ที่ใช้
`_live_session()`-style mock (busy-wait, supersede, unconfirmed, blocked-prompt,
auth-failure, fan-out, spawn-task-delivery ฯลฯ) เริ่มนับ boot-stall จากค่า Mock
truthy โดยไม่ได้ตั้งใจ → เพิ่ม `isinstance(_booting, bool)` guard เหมือนที่
`auth_failure_reason` ทำไว้แล้วสำหรับปัญหาเดียวกัน — รันชุด delivery test เดิมทั้งหมด
ผ่านหมดหลังแก้ (ดู "Tests" ท้ายไฟล์)

## #255 — delivery ที่ส่งไม่สำเร็จค้างแล้ว paste ซ้ำ

### 1. `takkub task cancel --role <r>`

`task_delivery.DeliveryManager.cancel_for_session()` มีอยู่แล้วและถูกต้อง (ยกเลิกทุก
delivery ที่ยัง non-terminal ของ `(project, pane, session_generation)` เดียวกัน) แต่
**ไม่มี CLI เรียกถึงเลย** — เพิ่ม:

- `Orchestrator.cancel_task_delivery(role, project=None)` (orchestrator.py,
  `command_surface` cluster ข้าง `task_show_info`) — resolve pane, generation ปัจจุบัน,
  เรียก `cancel_for_session`, เคลียร์ `_last_delivery_ids` entry ถ้ามี
- `cli_server.py`: `cmd == "task-cancel"` → เรียก method ข้างบน
- `cli.py`: `takkub task cancel --role <r>` subcommand, gate ด้วย
  `_require_lead_for_task_admin` เหมือน `task reconcile`/`task close` (pattern เดิม —
  gate ฝั่ง client เท่านั้น เช่นเดียวกับ 2 คำสั่งพี่น้อง ไม่ใช่ `_LEAD_ONLY_CMDS` /
  `_LEAD_SPOOF_GUARDED_CMDS` ฝั่ง server — คงความสม่ำเสมอกับ precedent เดิม ไม่ได้เพิ่ม
  server-side gate ใหม่ให้เฉพาะคำสั่งนี้)

### 2. `takkub send` auto-cancel เมื่อ Lead รับช่วงเอง

`Orchestrator.send()` เพิ่มเช็คก่อนเขียนข้อความ: ถ้า `to_role` มี delivery ค้างอยู่
(non-terminal) สำหรับ session generation ปัจจุบัน **และ** ผู้ส่งคือ Lead
(`from_role in (None, LEAD.name)` — `None` ครอบ CLI default ตอน Lead รันเอง) →
`cancel_for_session()` ทันที + แจ้ง Lead ด้วย `[delivery-superseded]` (#255)

**ตัดสินใจ: auto-cancel ไม่ใช่แค่เตือน** เหตุผล — warning-only ยังทิ้ง race window ไว้
(Lead ต้อง action เองก่อน pane กลับ ready) ซึ่งเป็นเงื่อนไขเดียวกับที่ทำให้เกิด incident
จริงตั้งแต่แรก (Lead กู้เองสำเร็จแล้ว แต่ delivery เดิมยังไม่ถูกยกเลิก) auto-cancel
ปิด race ทั้งหมดทันทีที่ Lead ส่งข้อความเข้า pane ตรงๆ — ปลอดภัยเพราะ
`cancel_for_session` ทำงาน scoped เฉพาะ `(project, pane, session_generation)`
เดียวกันเท่านั้น ไม่แตะ delivery ของ session generation ใหม่กว่า

จงใจ **ไม่** auto-cancel เมื่อผู้ส่งเป็น peer teammate (`from_role` เป็น role อื่นที่ไม่ใช่
lead) — ข้อความ peer-to-peer (`backend → qa`) ไม่ใช่สัญญาณว่า Lead กำลังรับช่วง delivery
เอง ยกเลิก delivery ของคนอื่นเพราะ teammate คุยกันเองจะเป็น regression ใหม่

### 3. `expire_stale()` — เดิมเป็น dead code (0 caller ทั้ง repo) + scope ผิด

ตรวจแล้วพบ 2 ปัญหาซ้อนกัน ไม่ใช่แค่ "เกณฑ์ TTL ไม่เหมาะ" อย่างเดียว:

**(a) ไม่มีใครเรียกเลย** — grep ทั้ง `src/` เจอ definition อย่างเดียว ไม่มี call site
ไหนเลยก่อนรอบนี้ แปลว่า behavior ที่ #255 อธิบาย ("delivery ที่ retry นานเกินควร expire
แล้วแจ้ง Lead") **ไม่เคยเกิดขึ้นจริง** ไม่ว่า TTL จะตั้งเท่าไหร่

**(b) scope เดิมกวาดกว้างเกินไป** — เช็คเดิม `state not in _TERMINAL_STATES` ครอบคลุม
`ACCEPTED`/`RUNNING`/`SPAWNED_IDLE` ด้วย ซึ่งหมายถึง "delivery ถึงมือแล้ว teammate
กำลังทำงานอยู่" — งานจริงรันได้เป็นชั่วโมง ถ้าเอา `expire_stale()` ไปผูก watchdog ตรงๆ
โดยไม่แก้ scope ก่อน จะกลาย เป็น auto-cancel task ที่กำลังรันอยู่จริงตาม TTL ที่นับจาก
เวลา **สร้าง** delivery ไม่ใช่เวลาที่ "ค้าง" จริง — regression ที่แย่กว่าปัญหาเดิม

แก้: เพิ่ม `_IN_FLIGHT_STATES` (`QUEUED`/`WAITING_RESOURCE`/`WRITING`/`WRITTEN`/
`SUBMITTING`/`UNCERTAIN` — เฉพาะ "attempt ยังไม่ยืนยันว่าถึงมือ") ให้ `expire_stale()`
กวาดเฉพาะกลุ่มนี้ ไม่แตะ `ACCEPTED`/`RUNNING`/`SPAWNED_IDLE` คืน `list[TaskDelivery]`
แทน `int` เดิม (ไม่มี caller เดิมพึ่ง return type — ปลอดภัย) เพื่อให้ reaper รู้
role/project ของแต่ละ delivery ที่เพิ่งหมดอายุ ไปสร้างข้อความแจ้ง Lead ได้ตรงตัว
(`reason="stale_reap"` แยกจาก `_reject_stale`'s `"stale_generation"` เดิม)

**(c) TTL default เดิม (30s) สั้นกว่า self-heal resend window ของตัวเอง** — พบระหว่าง
ตรวจเกณฑ์ตามที่ user ขอ: `_delayed_enter_verified`'s busy-resend budget
(`_SUBMIT_BUSY_MAX_RESENDS × _SUBMIT_VERIFY_GRACE_MS ≈ 150×600ms = 90s` — เอกสารในโค้ด
เองก็บอกว่าตั้งใจให้ครอบคลุม codex/agy 90s cold-boot allowance) ยาวกว่า TTL 30s เดิม
เกือบ 3 เท่า หมายความว่า resend ที่เกิดหลัง 30s แรกของ delivery เดียวกัน (ปกติมากสำหรับ
MCP boot ที่ใช้เวลาเกิน 30s) จะโดน `_reject_stale` (เรียกจาก `retry_enter`) ตีเป็น
`EXPIRED` ทั้งที่ยัง resend จริงอยู่ **และ** เสี่ยงโดน PTY writer thread's เอง
(`pty_session.py PtyWriter.run`) drop write ทิ้งเงียบๆ ถ้าดันไปติดคิว congestion —
คนละกลไกกับ #254 (ซึ่งไม่เคยถึง `_deliver()` เลย) แต่เป็น correctness bug จริงที่ latent
อยู่ก่อนรอบนี้ ปรับ default เป็น **120s** (env เดิม `TAKKUB_TASK_DELIVERY_TTL_SEC`) —
มี margin ~30s เหนือ worst-case 90s สั้นกว่า `BUSY_WAIT_CEILING_SEC` (1800s) มาก
ปรับ `orchestrator.py::send()`'s literal fallback (peer message TTL) ให้ตรงกันด้วย
(กลไกเดียวกัน ผ่าน `_delayed_enter_verified` เหมือนกัน) — **ไม่แตะ**
`lead_inbox.py`'s อีก 2 จุดที่อ่าน env เดียวกัน (Lead-notify pump digest delivery,
line ~2229/~2474) เพราะเป็นคนละ code path (plain write ไม่มี resend loop ต่อ —
TTL สั้นที่นั่นถูกแล้ว การยืดจะแปลว่า notice เก่าค้างอยู่ในคิวนานขึ้นเฉยๆ)

**(d) reaper wiring** — `LeadInboxMixin._reap_stale_deliveries()` (ใหม่, `lead_inbox.py`)
เรียก `expire_stale()` แล้ว `_notify_lead` ต่อ delivery ที่เพิ่งหมดอายุ (`[delivery-stale-reap]`,
issue #255) ต่อเข้า `Orchestrator._check_idle_teammates`'s tick เดิม (5s, ride ต่อจาก
`_reap_pending_done_notices()` — ไม่เพิ่ม QTimer ใหม่) รอบนี้จะจับ delivery ที่
poll loop ของมันเองยอมแพ้ไปแล้ว (`sent[0]=True` ทาง blind-deliver/timeout) แต่
DeliveryManager state ไม่เคยถูก set เป็น terminal — ช่องว่างเดียวที่ one-shot warning
ทั้งหมดใน `_check()` (busy-wait/boot-stall/unconfirmed) ไม่ครอบคลุม เพราะพวกนั้นทำงาน
ระหว่าง poll loop ยังรันอยู่เท่านั้น

## ไม่แตะ

- `src/agent_takkub/orchestrator.py`'s done-report / "files touched" evidence-scan
  sub-cluster (`_scan_done_evidence`, `_find_evidence_files`, `_evidence_stat_mtime`,
  `done()` ตัวมันเอง) — codex ทำ #251 คู่ขนานอยู่ ตามที่ task spec สั่ง
- `pty_session.py` — ไม่มี edit จริง (ลอง provider-spec boot-marker table แยกก่อน
  แล้ว revert ทิ้งหลังเจอ `shows_startup_marker()`/`_STARTUP_MARKERS` ที่มีอยู่แล้ว
  ตรงเงื่อนไขพอดี — ใช้ของเดิมแทนสร้างซ้ำ)
- `provider_spec.py` — เหตุผลเดียวกัน
- `_reject_stale()` (internal ของ `task_delivery.py`) — ยังเช็ค "any non-terminal
  state" เหมือนเดิม (ไม่จำกัดแค่ `_IN_FLIGHT_STATES`) เพราะถูกเรียกเฉพาะตอน
  write/submit/retry ของ delivery **เดียวกัน** เท่านั้น (`validate_for_write`,
  `begin_write`, `begin_submit`, `retry_enter`) ซึ่งไม่มี call site ไหนเรียกซ้ำหลัง
  delivery เข้า `ACCEPTED` อยู่แล้ว — เป็น dead branch สำหรับ state กลุ่มนั้นในทางปฏิบัติ
  ไม่จำเป็นต้องแก้เพื่อลด blast radius
- `_LEAD_ONLY_CMDS` / `_LEAD_SPOOF_GUARDED_CMDS` (`cli_server.py`) — `task-cancel`
  ตามรอย `task-reconcile`/`task-close` เดิมที่ก็ไม่อยู่ใน 2 set นี้เหมือนกัน (client-side
  gate เท่านั้น) ไม่ได้เพิ่มความเข้มงวดใหม่ให้เฉพาะคำสั่งนี้คำสั่งเดียว

## Multi-provider / cross-platform

- `shows_startup_marker()`/`_STARTUP_MARKERS` เป็น global text match ไม่ผูก provider
  ใดๆ (ตรงข้ามกับที่ตั้งใจแรกจะทำ per-provider table เฉพาะ codex) — ใช้ได้กับทุก
  provider ที่ render ข้อความ boot ในรูปแบบใกล้เคียงกัน โดยไม่ต้องแก้โค้ดเพิ่มถ้ามี
  provider ใหม่ในอนาคตที่ใช้คำเดียวกัน
- `BOOT_STALL_GRACE_SEC`/TTL bump เป็นตัวเลข env-overridable ล้วน ไม่มี string เฉพาะ
  platform หรือ path แยก Windows/macOS ไม่กระทบ cross-platform guardrail

## Tests

- `tests/test_delivery_boot_stall_notice.py` (ใหม่) — 7 tests: escalate once หลัง
  streak ต่อเนื่องผ่านเกณฑ์, ไม่ trip ให้ pane busy ธรรมดา, marker กระตุกๆ (intermittent)
  ไม่สะสมข้าม reset, unconfigured mock session ไม่ false-trip (regression guard),
  + direct tests ของ `_warn_lead_delivery_boot_stall` (เนื้อหา, no-op self-warn,
  no-op ไม่มี live lead)
- `tests/test_task_delivery_v2.py` — เพิ่ม 3 tests: `expire_stale()` กวาด in-flight
  ที่หมดอายุ, ไม่แตะ ACCEPTED/RUNNING, ไม่แตะ terminal state
- `tests/test_task_delivery_cancel_and_reap.py` (ใหม่) — 10 tests: `cancel_task_delivery`
  (cancel สำเร็จ/ไม่มีอะไรให้ cancel/unknown role/ไม่มี delivery manager), `send()`
  auto-cancel (lead cancel+notify / peer ไม่ cancel / ไม่มี delivery manager ไม่พัง),
  `_reap_stale_deliveries` (reap+notify / ไม่มีอะไร stale / ไม่มี delivery manager)
- `tests/test_task_reconcile_close_cli.py` — เพิ่ม 4 assertions/tests สำหรับ
  `task cancel` CLI surface (lead-gate, payload forwarding, failure exit code)
- targeted regression run ที่ผ่านทั้งหมดหลังแก้ (ไม่รัน full suite ตาม test-tier policy):
  `test_delivery_busy_wait_notice.py`, `test_delivery_supersede.py`,
  `test_delivery_unconfirmed.py`, `test_delivery_blocked_prompt.py`,
  `test_delivery_auth_failure.py`, `test_fan_out_delivery_race.py`,
  `test_spawn_task_delivery.py`, `test_peer_cc_durability.py`,
  `test_send_unknown_role_message.py`, `test_task_reconcile_orchestrator.py`,
  `test_task_show.py`, `test_cli_server*.py` (ทั้ง 6 ไฟล์), `test_spawn_queue_stuck.py`,
  `test_pty_ready_prompt.py`, `test_idle_watchdog.py`, `test_regression_findings_2026_06.py`
