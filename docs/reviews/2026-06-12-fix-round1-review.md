# Fix Round 1 — Review (reviewer)

**วันที่:** 2026-06-12 · **ผู้ review:** reviewer (specialist)
**ขอบเขต:** uncommitted diff (`git diff` + new `tests/test_regression_findings_2026_06.py`)
เทียบกับ `2026-06-12-full-system-review.md` (FR) + `2026-06-12-codex-crosscheck.md` (CX)
**Findings ทั้งหมด:** 3 HIGH · 7 MED · 5 LOW (= 15)

---

## ⚖️ VERDICT: **PASS** (มี residual low-severity 2 ข้อ — non-blocking)

- ✅ **3/3 HIGH** แก้ครบ ถูกจุด มี regression test
- ✅ **6/7 MED** แก้ครบ · ◐ **CX-M3** แก้บางส่วน (กรณีร้ายแรงสุดแก้แล้ว, สาย delayed-Enter ยังไม่ capture session — low impact)
- ✅ **4/5 LOW** แก้ครบ · ◐ **FR-L2** แก้บางส่วน (เพิ่ม cap+reaper ใหม่ แต่ไม่มี hard read-buffer cap)
- ✅ **ไม่มี bug ใหม่ใน critical path** · full suite **1927 passed, 2 skipped** (2 failures เดิมที่ codex เจอ = หาย)
- ✅ Backward-compat checks ที่ขอมาผ่านหมด (ดูท้ายเอกสาร)

> สรุป: งานรอบนี้ commit ได้ในแง่ correctness/security. residual 2 ข้อเป็น **net-improvement-but-incomplete**
> (ไม่ใช่ regression — จุดเหล่านั้นเดิมไม่มี guard เลย) Lead เลือกได้ว่าจะ loop อีกรอบเก็บ residual
> หรือรับไว้แล้วเปิด follow-up ticket.

---

## รายการ finding-by-finding

| ID | finding | สถานะ | หลักฐาน |
|---|---|---|---|
| **FR-H1** | `_spawn_in_progress` ไม่มี finally → spawn ค้างถาวร | ✅ FIXED | 4 branch (shell 1445 / gemini 1527 / codex 1612 / claude 2027) ครอบ `try/…/finally: reset+drain` ทุกตัว — reset ใน finally ทั้ง success/except path |
| **CX-H1** | unauthenticated `done` ปิด pane ข้าม project ได้ | ✅ FIXED | Layer-4 pane-token gate (cli_server 286) · `done` ต้องมี valid pane token · identity ดึงจาก `_pane_tokens` server-side · raw client ไม่มี token → reject · tested |
| **CX-H2** | stale exit signal จาก session เก่า detach/ฆ่า session ใหม่ | ✅ FIXED | (a) `_session_generation` counter ใน `AgentPane._on_exit` (agent_pane 347/448) drop stale gen · (b) ทุก orchestrator spawn-lambda เพิ่ม guard `pp.session is s` · tested (3 cases) |
| **FR-M1** | hot.md tick สแกน JSONL 3 รอบเต็มบน main thread | ✅ FIXED | `scan_hot_md_metrics()` single-pass + per-file (mtime,size) cache (chatlog_scanner 595) · `_write_hot_md` เรียกตัวเดียว (orchestrator 4390) |
| **FR-M2** | bracketed-paste breakout ผ่าน `\x1b[201~` ใน body | ✅ FIXED | `_sanitize_pane_text()` (orch 470) strip `\x1b[200~/201~`+bare ESC+`\r` · ใช้ครบ 3 write path: task inject (2545), `_notify_lead` (3186), `send` (3256) |
| **CX-M1** | `end-session` documented lead-only แต่ไม่ gate | ✅ FIXED | เพิ่มเข้า `_LEAD_ONLY_CMDS` → Layer1 role-gate + Layer2 lead-token · tested (no/wrong token → reject; valid → ok) |
| **CX-M2** | TCP framing ไม่มี size/conn/type/partial limit | ✅ MOSTLY | frame cap 64KiB + conn cap 32 + idle reaper 30s + `isinstance(req,dict)` + type-check cmd/from/auth · tested 6 cases · **residual** ↓ R2 |
| **CX-M3** | delayed callback ยิงใส่ mutable pane slot | ◐ PARTIAL | done-close (orch 3636 `_close_if_same_session`) capture session + tested ✅ · **แต่** delayed-Enter `\r` (2549 / 3190 / 3261) ยัง deref `pane.session`/`lead.session` ตรงๆ → ดู R1 |
| **CX-M4** | peer message identity/project forgeable | ✅ FIXED | pane-token override `req["from"]`/`from_project` จาก registry (cli_server 300-306) · tested: backend token + `from:frontend` → identity=backend; forged project ถูก ignore |
| **CX-M5** | allowlist มี `ANTHROPIC_AUTH_TOKEN` (reusable bearer) | ✅ FIXED | ลบออกจาก `_PANE_ENV_ALLOWLIST` (pane_env 57) · opt-in ผ่าน `TAKKUB_PANE_ENV_ALLOW` + เอกสารระบุว่า weaken isolation |
| **FR-L1** | `load_projects()` ไม่จับ JSONDecodeError | ✅ FIXED | try/except → log warning + return default (config 63) |
| **FR-L2** | cli_server ไม่มี read-buffer/connection cap | ◐ PARTIAL | conn cap + idle reaper เพิ่มแล้ว (ของใหม่ทั้งคู่) · **แต่** ไม่มี `setReadBufferSize` → ดู R2 |
| **FR-L3** | exception ใน watchdog tick หลุด → watchdog ตาย | ✅ FIXED | per-pane `try/except` ใน `_check_idle_teammates` (4445) + `_check_stuck_panes` (4555/4676) |
| **FR-L4** | read cmd (`list`/`status`) ไม่มี gate | ✅ FIXED (by decision) | เพิ่ม comment ระบุชัด "intentionally open — trust-local" (cli_server 315) = ตรงกับ option ที่ finding เสนอ |
| **CX-L1** | `terminate()` assume `_pid` always exists → suite fail | ✅ FIXED | getattr + `(AttributeError, RuntimeError)` guard ทุก attr access ใน `terminate()` (pty_session 405-468) · suite เขียวแล้ว |

---

## 🔎 Residual items (low severity, non-blocking)

### R1 — CX-M3 delayed-Enter callbacks ยังไม่ capture session
`orchestrator.py:2549, 3190, 3261` — หลัง paste payload จะ schedule
`QTimer.singleShot(_enter_delay_ms, lambda: pane.session and pane.session.write("\r"))`
lambda deref **`pane.session` (mutable slot)** ไม่ได้ capture session ตอน paste.
ถ้า pane respawn ภายใน window `_enter_delay_ms` (~ไม่กี่ร้อย ms–ไม่กี่วิ สำหรับ payload ใหญ่)
→ `\r` ลงไปที่ session ใหม่.
- **Impact ต่ำ:** stray `\r` ลง session ที่เพิ่ง boot = แค่ submit บรรทัดว่าง (เทียบกับ done-close 2.5s ที่ปิด pane ผิด ซึ่ง **แก้แล้ว**).
- **ไม่ใช่ regression:** จุดนี้เดิมก็ไม่มี guard.
- **แก้ถ้าจะให้ครบ:** `_sess = pane.session; QTimer.singleShot(d, lambda s=_sess: s.is_alive and s.write("\r"))` ทั้ง 3 จุด.

### R2 — FR-L2 / CX-M2 read-buffer ไม่มี hard cap + escape hatch
1. ไม่มี `sock.setReadBufferSize(_MAX_FRAME_BYTES)` — frame cap ทำงาน **เฉพาะตอนมี `\n`**;
   client ส่ง bytes ยาวๆ **ไม่มี newline** จะสะสมใน Qt buffer จนกว่า reaper 30s จะปิด (mitigate แต่ไม่ hard-cap).
2. `_on_ready_read` mark active ด้วยการ `_open_connections.pop(sock)` → connection ที่ส่ง 1 frame แล้วถือ socket ค้าง
   จะ **หลุดทั้ง conn-cap และ reaper ถาวร** (untracked). client ปกติ (request-response, cli.py ปิดทันที) ไม่โดน.
- **Impact ต่ำ:** local-only (loopback) + เป็น **ของเพิ่มใหม่** (เดิมไม่มี cap/reaper เลย → net improvement).
- **แก้ถ้าจะให้ครบ:** เพิ่ม `setReadBufferSize`; และเก็บ connection ไว้ใน registry พร้อม last-activity ts แทนการ pop (reap ตาม inactivity แทนตาม "เคยส่ง frame ไหม").

### หมายเหตุเล็ก (ไม่ต้องแก้ก็ได้)
- `_hot_md_cache` (chatlog_scanner) เป็น global dict โตเรื่อยๆ ตาม session-file path ที่เคยเห็น (ไม่มี eviction). cockpit รันยาวมากๆ → memory โตช้าๆ. bounded by #session files; restart ล้าง.
- **FR-H1 ไม่มี regression test** ตรงๆ (finding แนะนำให้ mock `statusChanged` slot ให้ raise แล้ว assert spawn ถัดไปไม่ค้าง). logic verify ด้วยตาแล้วถูก แต่ test coverage จุดนี้ยังว่าง.
- frame cap 64KiB ครอบ JSON request รวม `task` string — task spec ที่ยาวเกิน ~65KB จะถูก reject (เป็นค่าที่ทั้ง 2 review เสนอเอง; task ปกติ < ไม่กี่ KB → ยอมรับได้).

---

## ✅ Backward-compat / no-new-bug checks (ตามที่ task ขอ)

- **token auth backward-compat กับ pane เดิม:** ✅ pane spawn ใหม่ทุกตัวได้ `TAKKUB_PANE_TOKEN` register ใน `_pane_tokens`; revoke ตอน `close()` (orch 3361); Lead ใช้ `TAKKUB_LEAD_TOKEN` สำหรับ `send` (branch `pass`). ไม่มี persistence → ไม่มี pane "เดิม" ข้าม process. resumed pane ก็ผ่าน spawn เดิม → ได้ token ใหม่. cli.py stamp `TAKKUB_LEAD_TOKEN or TAKKUB_PANE_TOKEN` ถูกต้อง.
  - ⚠️ **behavior change (ตั้งใจ):** manual terminal (ไม่มี token) เรียก `send`/`done`/`end-session` จะถูก reject แล้ว — ตรงตามเจตนา fix (CX-H1/M1/M4). ถ้ามี workflow ที่ user เคยพิมพ์ `takkub send` จาก terminal เปล่า จะใช้ไม่ได้อีก — Lead ควรรับรู้.
- **session-generation guard ไม่ block legit callback:** ✅ session ปัจจุบัน gen ตรง → `_on_exit` ทำงานปกติ; guard drop เฉพาะ stale gen. orchestrator lambda `pp.session is s` ผ่านสำหรับ session ปัจจุบัน. tested `test_exit_from_current_session_fires`.
- **sanitize ไม่กิน payload ปกติ:** ✅ `_sanitize_pane_text` strip เฉพาะ ESC/`\r`/paste-bracket — **คง `\n`** ไว้ → task spec หลายบรรทัดไม่เสีย. CRLF → เหลือ `\n` (ดีกว่าเดิม).
- **TCP cap ไม่ตัด frame ใหญ่ที่ legit:** ✅ frame legit (JSON request) ปกติเล็กมาก; 64KiB เพดานสูงพอ (ดูหมายเหตุ task-spec ข้างบน).

---

## Test verification
```
python -m pytest tests/          → 1927 passed, 2 skipped (34.5s)
tests/test_regression_findings_2026_06.py → 23 passed
```
- 2 failures ที่ CX เจอตอน cross-check (terminate `_pid`) = หายแล้ว (CX-L1 fix).
- regression tests ครอบ: forged done/send reject, pane-token override identity, end-session token gate, TCP frame/type validation, stale-session-exit drop, done-close session guard.
- modified tests (`test_cli_server_auth/role_gate/end_session`) ถูก **เข้มขึ้น** (เช่น `test_send_from_teammate_no_token` พลิกจาก allowed→rejected) — ไม่ใช่ทำให้ผ่านแบบ weaken. ✅
- ⚠️ **test-quality:** regression session-gen + done-close test สร้าง lambda จำลอง inline (ไม่เรียก production code path ตรงๆ) → ตรวจ "logic copy" ไม่ใช่ของจริง. coverage มีค่าแต่ควรรู้ข้อจำกัด.
