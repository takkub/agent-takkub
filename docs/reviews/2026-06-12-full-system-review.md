# Full-System Code Review — agent-takkub (reviewer)

**วันที่:** 2026-06-12 · **ผู้ review:** reviewer (specialist)
**ขอบเขต:** `src/agent_takkub/` + `cli` (= `cli.py`/`cli_server.py`) + `tests/`
**โฟกัส:** correctness bugs + security (race conditions, IPC/TCP-JSON edge cases,
subprocess/env/path handling, error handling, pane lifecycle)
**กันซ้ำ:** ตัด finding ที่ทับกับ `docs/reviews/2026-06-03-improvement-audit.md`
ออกแล้ว (shard timeout/generation, pop-before-confirm, send forgery + raw ESC
short-path, main-thread git-status/scan_artifacts/QWebEngine, marker-scraping
fragility, events.log write-only, `--shards` clamp ฯลฯ). รายการด้านล่างคือสิ่งที่
**audit เดิมไม่ได้ครอบคลุม** หรือเป็น **กลไกใหม่** ของ cross-cut เดิม (ระบุชัดเมื่อ
เป็นกรณีหลัง)

> ภาพรวม: codebase ผ่านการ audit หนักมาก — guard/comment/test แน่นผิดปกติ ผม
> หาช่องที่ยัง "ใหม่จริง" ได้ไม่มาก สิ่งที่เจอเด่นสุดคือ **state-flag leak ที่ทำให้
> spawn ค้างถาวร** (ตรงกับ failure-class "cockpit spawn ค้าง" ที่โปรเจกต์แคร์ที่สุด)

---

## 🔴 HIGH

### H1. `_spawn_in_progress` ไม่มี `finally` — exception ใน post-spawn bookkeeping ทำให้ spawn arbiter ค้างถาวร (ทุก pane ต่อจากนั้น spawn ไม่ได้)
**ไฟล์:** `src/agent_takkub/orchestrator.py`
- shell branch: `1412` (set True) → `1428` (set False) ; success path `1419–1427` ไม่ถูกป้องกัน
- gemini branch: `1486` → `1503`
- codex branch: `1563` → `1583`
- claude branch: `1967` → `2022` ; bookkeeping `1975–2021` ไม่ถูกป้องกัน

**ปัญหา:** ทั้ง 4 branch มีแพตเทิร์นเดียวกัน:
```python
self._spawn_in_progress = True
try:
    session.spawn(...)          # ← มีแค่ตรงนี้ที่อยู่ใน try/except
except Exception as e:
    self._spawn_in_progress = False
    self._drain_spawn_queue()
    return False, ...
pane.attach_session(session, ...)          # ← นอก try
session.processExited.connect(...)
self._auto_trust(role_name, ...)           # อาจ raise
self.statusChanged.emit()                  # slot (main_window) ถ้า raise → propagate
...
self._spawn_in_progress = False            # ← ถ้าโค้ดข้างบน raise จะไม่ถึงบรรทัดนี้
self._drain_spawn_queue()
```
ถ้า **อะไรก็ตามระหว่าง `attach_session` กับบรรทัด reset สุดท้าย raise** (slot ที่
ต่อกับ `statusChanged`/`paneResumed` เป็น direct connection บน main thread →
exception propagate กลับมาที่ `emit()`; `_auto_trust`; `_log_event`) →
`_spawn_in_progress` ค้างเป็น `True` **ตลอดไป**. หลังจากนั้น `spawn()` ทุกครั้งจะเข้า
FIFO branch (`1368`) แล้ว enqueue เงียบๆ ไม่มีใคร reset flag → **cockpit spawn pane
ใหม่ไม่ได้อีกเลยจนกว่าจะ restart**. นี่คือ failure-class เดียวกับ freeze/spawn-stuck
ที่ memory `project_cockpit_freeze_spawn_rca` บันทึกไว้ แต่เป็น root ใหม่ (state leak
ไม่ใช่ COM/GIL).

**แนวแก้:** ครอบ post-spawn bookkeeping ด้วย `try/finally` ที่ reset flag + drain
เสมอ (ทั้ง 4 branch) — เช่น hoist `self._spawn_in_progress = False;
self._drain_spawn_queue()` เข้า `finally` ตัวเดียวที่ครอบตั้งแต่ `session.spawn()`:
```python
self._spawn_in_progress = True
try:
    session.spawn(...)
    pane.attach_session(...)
    ... bookkeeping ...
    return True, ...
except Exception as e:
    return False, f"failed to spawn: {e}"
finally:
    self._spawn_in_progress = False
    self._drain_spawn_queue()
```
+ test: mock `statusChanged` slot ให้ raise แล้ว assert ว่า spawn ถัดไปไม่ค้างใน queue.

---

## 🟠 MEDIUM

### M1. hot.md tick (ทุก 60s, Qt main thread) สแกน session JSONL ทุกไฟล์ **3 รอบเต็ม** ข้ามทุกโปรเจกต์
**ไฟล์:** `orchestrator.py:4291 `_write_hot_md` → `4319–4322` ; `chatlog_scanner.py:314 / 392(count_tool_retries) / 551`

`_write_hot_md` ถูกเรียกโดย `_HOT_MD_INTERVAL_MS = 60_000` timer บน main thread และเรียก:
```python
count_hook_fires(since=start_of_today)        # full pass
count_user_corrections(since=start_of_today)  # full pass
count_tool_retries(since=start_of_today)      # full pass
```
แต่ละตัวเรียก `iter_session_files()` **โดยไม่ส่ง `project_filter`** → วนทุกโปรเจกต์,
เปิด+parse ทุก `.jsonl` ที่ mtime ≥ start-of-today **ใหม่ทั้งหมดทุกตัว** (3 รอบ).
Claude session ไฟล์โตได้ถึงหลายสิบ MB; วันที่งานหนัก = อ่าน+`json.loads` หลายร้อย MB
× 3 ทุกนาที **บน Qt main thread** → UI สะดุดเป็นจังหวะ และซ้ำเติม freeze-class เดิม.

> นี่เป็น **call-site ใหม่** ของ cross-cut D (main-thread blocking) ที่ audit เดิม
> ระบุเฉพาะ git-status / scan_artifacts / QWebEngine — ไม่ได้แตะ hot.md scan.

**แนวแก้ (เลือกได้):**
1. รวม 3 ตัวเป็น single-pass: เปิดแต่ละไฟล์ครั้งเดียว แล้วนับทั้ง 3 metric ใน loop เดียว
   (ลด I/O 3×→1×).
2. ย้ายไป QThreadPool worker (เหมือน update_worker) แล้ว push ผล hot.md กลับ main thread.
3. cache ผลต่อไฟล์ด้วย (path, st_mtime, st_size) — ไฟล์ที่ไม่เปลี่ยนข้ามรอบ tick ข้ามได้.

### M2. Bracketed-paste **breakout** ผ่าน `\x1b[201~` ที่ฝังใน message body (ต่อยอด cross-cut E)
**ไฟล์:** `orchestrator.py:470 _paste_payload` ; ใช้ใน `send()` `3187`, `_notify_lead` `3117`, task inject `2475`

audit cross-cut E พูดถึงเฉพาะ **short path (<200 char เขียน raw)**. แต่ long path
(≥200 char) ที่ห่อด้วย `\x1b[200~ … \x1b[201~` ก็ **ไม่ strip `\x1b[201~` ออกจาก body**
ก่อนห่อ. body ที่ผู้ส่ง (peer pane ใดก็ได้ที่อ้าง role ไม่ใช่ lead — ไม่ต้อง token,
ดู cli_server `_LEAD_SPOOF_GUARDED_CMDS` คุมเฉพาะ `from: lead`) แทรก `\x1b[201~`
จะ **ปิด bracket ก่อนเวลา** ทำให้ส่วนที่เหลือถูกตีความเป็น input/control-sequence
ของ agent ที่รัน `--dangerously-skip-permissions`. กล่าวคือ **กลไกป้องกันเดียว
(bracketed paste) ของ long path ถูก bypass ได้** — สรุปแล้วทั้ง short และ long path
ต้องการการแก้แบบเดียวกัน.

**แนวแก้:** ก่อน `_paste_payload`/เขียน PTY — strip/escape control bytes จาก body:
อย่างน้อย `\x1b` (และ `\r`/`\n` ที่ไม่ตั้งใจ) และโดยเฉพาะ substring `\x1b[201~`
และ `\x1b[200~`. รวมเป็น helper เดียว `_sanitize_pane_text()` ใช้ทุก write path
ที่รับ body จาก CLI/peer.

---

## 🟡 LOW

### L1. `load_projects()` ไม่จับ `JSONDecodeError` — projects.json พังทำ cockpit crash ทุก config-read
**ไฟล์:** `config.py:60–63`
```python
def load_projects() -> dict:
    if not PROJECTS_JSON.exists():
        return {"active": None, "projects": {}}
    return json.loads(PROJECTS_JSON.read_text(encoding="utf-8"))  # ← ไม่จับ error
```
`_write_json_atomic` กัน partial write ภายในได้ แต่ CLAUDE.md ระบุให้ **Lead แก้
projects.json ด้วยมือ** — typo เดียว (comma เกิน/ขาด) → ทุก `active_project()`,
`default_cwd_for_role()`, `lead_cwd()`, `get_open_tabs()` raise → spawn/route/startup
พังหมดด้วย stack trace ดิบ. **แนวแก้:** จับ `JSONDecodeError` → return default + log
warning ที่ status bar ("projects.json invalid, falling back") เพื่อ degrade แทน crash.

### L2. `cli_server` ไม่มี read-buffer cap / connection cap — local DoS
**ไฟล์:** `cli_server.py:130 _on_ready_read` (+ `_on_new_connection`)
`while sock.canReadLine()` ดึงทีละบรรทัด แต่ถ้า client เปิด socket แล้วส่ง bytes
**โดยไม่มี `\n`** → `canReadLine()` false ตลอด, data สะสมใน QTcpSocket read buffer
ไม่จำกัด; เปิดหลาย connection พร้อมกันก็ไม่มีเพดาน. local-only (loopback bind ถูกต้อง
แล้ว `113`) จึง severity ต่ำ แต่ควรตั้ง `sock.setReadBufferSize(64*1024)` + ปิด
connection ถ้า 1 บรรทัดเกิน N KB หรือไม่มี `\n` ภายใน timeout.

### L3. Exception ใน QTimer tick callback หลุดออกจาก idle/stuck watchdog
**ไฟล์:** `orchestrator.py:4343 _check_idle_teammates` (+ `_check_stuck_panes`)
เส้นทาง `_rate_limit_suppressed` → `rate_limit_reset_at` → `pty_session._parse_rate_limit_reset`
เรียก `time.mktime(target)` ซึ่ง **อาจ raise `OverflowError`/`ValueError`** ในเคส DST
boundary/ค่าเพี้ยน. `_check_idle_teammates` ไม่มี try ครอบ body ต่อ-pane — exception
หลุดออกจาก timer slot. ใน PyQt6 รุ่นใหม่ unhandled exception ใน slot อาจถูกส่งเข้า
`sys.excepthook` และ (ขึ้นกับเวอร์ชัน) ทำให้ tick ตัวนั้นตายเงียบ → watchdog หยุดทำงาน.
**แนวแก้:** ครอบ per-pane loop body ด้วย `try/except Exception: _log_event(...)` เพื่อให้
pane ตัวเดียวพังไม่ล้มทั้ง watchdog (มี pattern นี้แล้วใน `_feed_and_log`/`_write_hot_md`).

### L4. read-only IPC commands (`list`, `status`) ไม่มี gate ใดๆ — local process อ่าน pane state ได้
**ไฟล์:** `cli_server.py:272 (list) / 283 (status)` เทียบ `_LEAD_ONLY_CMDS` (`29`)
`harvest`/`harvest-done` อยู่ใน lead-only แล้ว แต่ `list`/`status` ไม่ถูกคุม — process
ใดบนเครื่องที่อ่าน `runtime/port` ได้ ก็ query สถานะ/stall ของทุก pane ทุกโปรเจกต์ได้.
ภายใต้ threat-model "trust all local" ถือว่ายอมรับได้ — แต่ตรงนี้ผูกกับ **การตัดสินใจ
threat-model ที่ cross-cut E ค้างไว้**: ถ้าเลือก token-ทุกคำสั่ง ต้องรวม read commands
ด้วย; ถ้าเลือก trust-local ให้เขียน comment ระบุชัดว่า read = intentionally open.

---

## ✅ จุดที่ตรวจแล้วผ่าน (ยืนยัน ไม่ใช่ปัญหา)
- `validate_name()` (`config.py:14`) กัน path-traversal + shard suffix รัดกุม; `spawn()`
  เรียก `_cwd_within_project()` (`orchestrator.py:1332`) กัน cwd หลุด project paths.
- ทุก subprocess (`pty_session._tree_kill`, `issues._gh`, `done()` git-status) เป็น
  **list-argv ไม่มี `shell=True`** + มี `timeout` → ไม่มี command-injection.
- env allowlist (`pane_env.py`) กัน secret รั่วเข้า teammate panes; `TAKKUB_LEAD_TOKEN`
  inject เฉพาะ Lead (`orchestrator.py:1732`); token เทียบด้วย `secrets.compare_digest`.
- `_write_json_atomic` (tmp+replace) กัน partial-write corruption.
- `token_meter` tail-scan + full-scan fallback, path-encode ตรงกับ Claude Code; ปลอดภัย.
- cli `_request` อ่านจนเจอ `\n` (`cli.py:75`) ไม่ตัด response กลางคัน.

---

## ลำดับแนะนำ
1. **H1** — `try/finally` รอบ post-spawn bookkeeping (4 branch) + regression test. แก้ก่อน:
   impact = spawn ค้างถาวร, ตรงกับ failure-class ที่เจ็บสุด.
2. **M2 + L4** — รวมเป็น 1 workstream "ปิด cross-cut E": `_sanitize_pane_text()` helper +
   ตัดสินใจ threat-model read commands ในรอบเดียว.
3. **M1** — single-pass hot.md scan (หรือย้าย worker) — cheap, ลด UI stutter ราย 60s.
4. **L1, L3** — robustness hardening (จับ error ที่ config-read + watchdog tick).
5. **L2** — buffer/connection cap (ต่ำสุด).
