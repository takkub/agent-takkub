# #322 — limit-stall runbook: pane waiting for usage-limit reset

Re-scoped by user directive (issue comment): ปิดใน 1 เวอร์ชันด้วย simulated
evidence ที่จำเป็น — **การ confirm auto-continue จริงกับ Claude Code 2.1.234+
ครั้งถัดไปเป็น follow-up note ไม่ใช่ blocker การปิดใบนี้.**

## หลักฐานจริง — 2026-08-19 20:20 checkpoint

Transcript จริง (ANSI stripped) จาก
`runtime/sessions/2026-08-19/agent-takkub/lead-205813.transcript.log`
(Lead pane) และ `.../backend-201637.transcript.log` (backend pane, ชนพร้อมกัน):

```
You've hit your session limit · resets 9:10pm (Asia/Bangkok) · progress saved
Press ⏎ to continue after reset
/upgrade or /usage-credits to finish what you're working on.
```

(backend pane เห็นแค่คำเตือน 95% ก่อนหน้านั้น: `You've used 95% of your
session limit · resets 9:10pm (Asia/Bangkok) · /upgrade to keep using Claude
Code` — แล้ว `takkub done "ยังไม่เสร็จ (usage limit hit ระหว่างทำ)"` ตัวเองก่อนจะชน
ตัวบล็อกจริง)

ผลจริง: **ไม่มี `lead-2*.transcript.log` ใหม่จนถึง `lead-065628.transcript.log`
เช้าวันถัดไป (2026-08-20 06:56)** — แปลว่า pane ไม่ได้กลับมาทำงานเองข้ามคืน แม้
banner จะบอก "Press ⏎ to continue after reset" ก็ตาม (ต้องมีคน enter จริงๆ — CLI
เวอร์ชันที่รันคืนนั้นยังเป็นพฤติกรรมแบบ manual-continue, ไม่ใช่ 2.1.234+ auto-continue
ที่ issue อ้างถึง). นี่คือ pain ตัวจริงที่ issue #322 อธิบาย ("ทั้ง wave ค้างจน limit
reset แล้วต้องปลุกเอง").

## สถานะ infra ที่มีอยู่แล้ว (ตรวจสอบแล้ว ไม่ใช่ของใหม่)

โครงสร้างนี้ครบและทำงานถูกต้องกับ pattern จริงข้างต้นอยู่แล้ว **ก่อน** งานชิ้นนี้:

- **detection (signal a):** `GENERIC_QUOTA_MARKERS` (`provider_spec.py`) มี
  `"hit your session limit"` อยู่แล้ว → `_parse_rate_limit_reset()`
  (`pty_session.py`) match ทั้ง banner จริงข้างบนได้ (regex เวลา `resets 9:10pm`
  ก็ parse ถูก) — ปักหมุดเป็น regression test ใหม่
  (`test_real_incident_banner_2026_08_19`, `tests/test_rate_limit_watchdog.py`)
- **detection (signal b):** `limit_autoresume._usage_confirms_limit()` เช็ค
  telemetry `five_hour` window ≥ 95% แยกจาก banner text — กัน false-positive
- **watchdog ไม่ respawn:** `orchestrator.py`
  - `_check_stuck_panes` บรรทัด `if (...).rate_limited_until > now: continue`
    — ไม่มีทาง auto-recover/close→respawn ทับ pane ที่ยังรอ limit
  - `_check_idle_teammates` เรียก `_rate_limit_suppressed()` ก่อน แล้ว
    `continue` — ไม่มีการยิง idle-nudge/reminder ทับ pane ที่ parked
- **park/wake:** `limit_autoresume.py` (`AutoResumeMixin`, epic #301) —
  park pane เมื่อ signal (a)+(b) ยืนยันตรงกัน, wake ผ่าน `QTimer.singleShot`
  ที่ `reset_at + WAKE_BUFFER_S` (3 นาที), มี cap รอบ + relimit-grace กันวนซ้ำ,
  เขียน progress marker ลงดิสก์ทุก transition (#158) กัน task หายถ้า pane ตายก่อน
  report `takkub done`

**สรุป:** งานส่วน (1)/(2) ของ scope เดิม (watchdog/done-gate ไม่ respawn/force
ทับ pane ที่รอ limit) **มีอยู่แล้วและ verify ผ่านกับ pattern จริง** — ไม่ต้องแก้อะไร
เพิ่มตรงนั้น

## ช่องว่างที่พบและแก้จริง — `_wake_parked_pane` race กับ auto-continue

`_wake_parked_pane()` (เดิม) เขียน nudge text + Enter เข้า pane แบบไม่มีเงื่อนไข
ที่เวลา `reset_at + WAKE_BUFFER_S` เสมอ — ออกแบบมาสำหรับ CLI รุ่นที่ต้อง
manual-continue (ตามที่ transcript จริงข้างบนพิสูจน์ว่าเป็นพฤติกรรมจริง ณ
2026-08-19)

Claude Code 2.1.234+ (ตาม context ของ issue) auto-continue เทิร์นที่ค้างเอง
ทันทีที่ window reset — ถ้า cockpit ยังเขียน nudge แบบเดิมโดยไม่เช็คก่อน จังหวะ
`reset_at + 3นาที` อาจตกใส่ pane ที่ CLI auto-continue ไปแล้วและกำลัง generate
อยู่พอดี (race แบบเดียวกับ A3 draft-hold — ข้อความ/Enter หลุดเข้ากลางเทิร์นที่กำลัง
ทำงาน แทนที่จะเข้า input ว่างๆ)

### ที่แก้ (`src/agent_takkub/limit_autoresume.py::_wake_parked_pane`)

ก่อน inject nudge, re-check signal (a) สดๆ อีกครั้ง (`pane.session.
rate_limit_reset_at(ps.quota_provider)`):

- **banner หายแล้ว** (CLI auto-continue ไปเองแล้ว) → skip nudge ทั้งหมด, แค่ clear
  state + เขียน progress marker (`reason="cli_auto_continued"`) + แจ้ง Lead
  ด้วย note ใหม่ `limit_resumed_self` (แยกจาก `limit_resumed` เดิม ให้ Lead
  แยกออกว่า cockpit ทำงานหรือ CLI ทำงานเอง)
- **banner ยังอยู่** (CLI รุ่น manual-continue, หรือยังบล็อกจริง) → เดินเส้นทางเดิม
  ทุกอย่าง (nudge + delayed Enter) — พฤติกรรมเดิม 100% ไม่เปลี่ยน
- **re-check เอง error** (session ถูก tear down ระหว่างนั้น ฯลฯ) → fail-safe เป็น
  "still_limited=True" → เดินเส้นทางเดิม ไม่มีทางค้าง parked เงียบๆ

Targeted tests (`tests/test_limit_autoresume.py`):
`test_wake_skips_nudge_when_cli_already_auto_continued`,
`test_wake_still_limited_falls_back_to_legacy_nudge`,
`test_wake_recheck_error_fails_safe_to_legacy_nudge`.

## V2 alignment — core scheduler contract

`src/agent_takkub/core/scheduling/runtime_control.py::RunState` เพิ่ม
`LIMIT_WAIT` เป็น state แยกจาก `PAUSED` — `park_for_limit(reset_at)` /
`resume_from_limit()` ไม่ปะปนกับ `pause()`/`resume()` ของ operator (resume()
ธรรมดาต้องไม่ปลุก pane ที่ยัง limit-wait อยู่, และ resume_from_limit() ต้องไม่ไป
เคลียร์ pause ที่ operator ตั้งใจกดเอง) `may_dispatch_new()` คืน False ระหว่าง
LIMIT_WAIT เหมือน PAUSED โดยอัตโนมัติ (ไม่ต้องเพิ่ม branch) เก็บ `limit_reset_at`
ไว้ให้ scheduler ฝั่ง V2 อ่านได้โดยตรงแทนที่จะพึ่ง watchdog-only
`PaneState.rate_limited_until` ต่อไปเมื่อ `TAKKUB_V2_SCHEDULER` เปิด — ปัจจุบันยัง
เป็น pure vocabulary เฉยๆ (ไม่มี caller จริงเรียก `park_for_limit`/
`resume_from_limit` จาก orchestrator เพราะ flag ยังปิดโดย default; V1 watchdog
path (`_rate_limit_suppressed`/`limit_autoresume.py`) ยังเป็นเส้นทางที่ใช้งานจริง
ทั้งหมด — สอดคล้องกับ pattern เดิมของ `core/scheduling` ทุกไฟล์ที่ REUSE_VS_REWRITE
กำหนดไว้)

Tests: `tests/test_core_scheduling.py` — `test_runtime_control_limit_wait_*`
(park/resume ไม่ปนกับ pause/resume ธรรมดา, terminal states ignore park, cancel
จาก LIMIT_WAIT ได้)

## LimitStore/usage UI

`src/agent_takkub/usage_meter.py::_provider_body_entries` — บรรทัด `5h` เพิ่ม
hint `· pane จะทำงานต่อเอง (auto-continue)` เมื่อ utilization ของ five_hour window
≥ `auto_resume.CONFIRM_UTILIZATION_PCT` (95%, threshold เดียวกับที่
auto-resume ใช้ยืนยัน signal (b) อยู่แล้ว — single source of truth ไม่มี
constant ซ้ำ) เฉพาะ `five_hour` เท่านั้น (`seven_day` ไม่ใช่ window ที่บล็อก pane
กลางงาน) Tests: `tests/test_usage_meter_bars.py` —
`test_five_hour_near_limit_shows_auto_continue_hint`,
`test_five_hour_below_threshold_no_auto_continue_hint`.

## สิ่งที่ยังไม่ยืนยัน (follow-up ตาม re-scope — ไม่ใช่ blocker)

- ยังไม่มี transcript จริงของ Claude Code 2.1.234+ ที่แสดง auto-continue เกิดขึ้น
  จริงในเคส limit-hit จริงครั้งถัดไป — งานชิ้นนี้ปิดด้วย simulated evidence
  (`rate_limit_reset_at()` คืน `None` ที่เวลา wake) ตามที่ user อนุมัติไว้ใน
  re-scope comment
- เมื่อเจอเหตุการณ์จริงครั้งถัดไป: ยืนยันว่า `note="limit_resumed_self"` ถูกยิงจริง
  ใน `runtime/events.log` (`pane_limit_resumed_by_cli`) แทนที่จะเป็น
  `limit_resumed` ธรรมดา — ถ้า CLI เวอร์ชันที่ใช้งานจริงยัง manual-continue อยู่
  (เหมือน 2026-08-19) ก็ยังปลอดภัย เพราะ fallback เดินเส้นทางเดิมทุกอย่าง
