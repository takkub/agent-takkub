# ma 2026-10-22 — "UI ค้างนานเกิน 10 วิ" ×4 ใบ (#884–#887) คืออะไรจริงๆ

สรุป: ทั้ง 4 ใบเป็น **ทั้ง process ถูกเครื่องแช่** ระหว่าง batch ที่มี Chrome e2e + 2–3 pane ไม่ใช่โค้ด cockpit เส้นใดติด I/O บน UI thread · ตัวจำแนก (`whole_process`) เข้มเกินจึงเปิดใบผิดประเภท · แก้ให้ `takkub ma` และ auto-issue ใช้คำตัดสินเดียวกัน

## หลักฐาน (prod `~/.agent-takkub/runtime/events.log`, 922 main_thread_stall records, 10-14 → 10-22)

| | |
|---|---|
| stall ≥ 10 s | 47 (10-14 ×4 · 10-15 ×3 · **10-16 ×35** · 10-18 ×5 · หลังจากนั้น 0) |
| อยู่ภายใน ±120 s ของ `watchdog_clock_sleep_gap_absorbed` (gap 30–98 s) | 31 / 47 |
| `watchdog_gap_ms` ≥ 2,000 (watchdog thread เองไม่ได้รัน) | 44 / 47 — แต่ติด `whole_process` แค่ 8 (เกณฑ์เดิม gap ≥ 80 % ของ peak) |
| duration ชนเพดาน ~29.9 s | 6 ครั้ง = episode ที่ยาวกว่า `WATCHDOG_SLEEP_GAP_THRESHOLD_S` (30) ถูก absorb เป็น sleep gap ส่วนที่สั้นกว่าหลุดมาเป็น "stall" |
| top frame บน main thread ของ 47 ครั้ง | กระจาย: `notify._codex_day_dirs` 9 · `shutil.which` (find_codex_executable) 6 · `pathlib.stat` 6 · pyte render 5 · `app.main` 4 · deepcopy/role_messages 2 · อื่นๆ 15 — ไม่มี hot path เดียว และ `stat` ครั้งเดียว 25 s เป็นไปไม่ได้ถ้า process ไม่ถูก starve |
| บริบททุก episode | pane `native_chrome` (qa/frontend e2e ผ่าน CDP) + 2–3 pane · realmforge-online 10-16/10-18 · unirecon 10-15 · saas_admin_amb 10-14 (198 stall ใน 45 นาที) |
| #887 | เครื่อง HQNB6708-390 (ไม่มี log ที่นี่) stall เดี่ยว 16.5 s — จัดเป็นชั้นเดียวกันตาม pattern ตรวจเพิ่มไม่ได้ |

สิ่งที่ยังเป็นงานโค้ดจริง (stall 2–4.5 s, watchdog ตรงเวลา, วันปกติ 10-19 → 10-22): `usage_shared.claude_identity` (read_text บน UI thread ×29), `find_codex_executable` → `shutil.which` ทุกครั้ง (×2 + 3.5 s วันนี้), `notify._resolve_codex_jsonl_path` → เปิด rollout (×3), `skill_learning/store.list_skills` read_text (3.6 s) — ไม่ได้แก้รอบนี้ (คนละเรื่องกับใบ) ควรเปิดใบแยกเมื่อเห็นซ้ำ

## ที่แก้ (2.2.23)

1. `app._StallTracker` — watchdog หลับ ≥ 2 s (`_WHOLE_PROCESS_GAP_ABS_S`) = `whole_process` ไม่ว่าสัดส่วน
2. `maintenance.stall_is_process_pause(rec, ts, pause_marks)` — คำตัดสินเดียว: `whole_process` / `watchdog_gap_ms ≥ 2000` (record เก่า) / อยู่ภายใน ±120 s ของ sleep-gap marker · ใช้ทั้งใน `scan_events` (ma) และ `auto_issue_signals.scan_for_signals` (rule `skip_process_pauses=True` บน stall ทั้ง 2 rule)
3. `takkub ma` ไม่นับ stall เหล่านี้เป็น 🟡 UI ค้าง อีก — แยกบรรทัด "ทั้ง process หยุด (เครื่องอิ่ม/sleep/OS throttle — ไม่ใช่โค้ด cockpit ติด I/O)" + `data.process_paused`

ตรวจกับสำเนา log prod จริง: scan ณ เวลาที่ #884/#885/#886 ถูกเปิด → **ไม่เปิดใบ** ทั้ง 3 จุด · control: stall 25.5 s ของ codex rollout scan 10-15 12:34 (gap 32 ms, ก่อน #882) **ยังเปิดใบ** · ma 120 h: UI ค้าง 782 → 754, process หยุด 140 → 168, UI ค้างนานสุด 29.9 → 25.6 s

## ของแถม

- `tests/test_usage_shared.py::test_concurrent_writers_lose_no_updates` flaky บน CI Windows (PR #889: `assert 86 == 90`) — `_file_lock` รอ 2 s แล้ว best-effort → เพิ่ม `threading.Lock` ระดับ process หน้า file lock · รัน 20 รอบผ่าน 20
- #890 (ใหม่): dev cockpit ปัด `takkub assign` ทุกครั้งด้วย "settings changed" ทั้งที่ CLI คำนวณ revision ตรงกัน — ตัด env/storage-root ออกแล้ว เหลือ in-memory state ของ orchestrator (ต้อง probe ฝั่ง server)
- สาเหตุต้นทาง (เครื่องแช่ 30–98 s ระหว่าง batch Chrome e2e) อยู่นอก cockpit — ถ้าเจออีกให้ดู RAM/WSL ตอนนั้นด้วย `machine-state.json` (`ram_percent`/`overloaded`)
