# Fix Round 2 — Review (reviewer)

**วันที่:** 2026-06-12 · **ผู้ review:** reviewer (specialist)
**ขอบเขต:** uncommitted diff (cumulative round1+round2) ของ `orchestrator.py`, `cli_server.py`,
`pty_session.py`, `config.py`, `agent_pane.py`, `chatlog_scanner.py`, `pane_env.py`, `cli.py` + tests
**เทียบกับ:** `2026-06-12-fix-round1-codex.md` (codex VERDICT=FAIL, 4 blocking) +
`2026-06-12-full-system-review.md` + `2026-06-12-codex-crosscheck.md`
**โฟกัสตามที่ Lead สั่ง:** trust boundary — (1) token revoke ordering · (2) TCP cap/reaper ·
(3) codex stale-session guard · (4) delayed-Enter TOCTOU

---

## ⚖️ VERDICT: **PASS** (residual 2 ข้อ = LOW, non-blocking)

ทั้ง **4 blocking findings ของ codex round1 ถูกแก้ตรงจุด** และ test ตอนนี้ยิงเข้า
**production code path จริง** (ไม่ใช่ local lambda เลียนแบบเหมือนรอบ1 ที่ codex ตำหนิ).

- ✅ **codex stale exit** → guard เพิ่มแล้ว (drop ก่อน `_on_session_exit` + crash-dump)
- ✅ **token lifecycle** → revoke-before-register ordering + revoke ครบ exit/close/spawn-fail/respawn
- ◐ **delayed-Enter** → helper `_delayed_enter` + แก้ทุก hot path · **เหลือ 1 cold watchdog path (4580)**
- ✅ **TCP cap bypass** → track-until-disconnected + reaper + bounded readLine + bytesAvailable check
- ✅ full suite **1947 passed, 2 skipped** (เพิ่ม ~20 test จาก 1927; 0 fail)

> residual ทั้ง 2 เป็น **LOW** (loopback-only / cold path / diagnostic-loss เท่านั้น ไม่ใช่
> lifecycle/security regression). Lead commit ได้ในแง่ correctness/security; เก็บ residual
> เป็น follow-up หรือ loop เก็บอีกรอบก็ได้.

---

## (1) Token revoke ordering — ✅ PASS

**ordering ทุก branch ถูกต้อง: revoke old ก่อน register new** (ไม่มี window 2 token valid พร้อมกัน):
```python
env["TAKKUB_PANE_TOKEN"] = _tok
for _t in [t for t,v in list(self._pane_tokens.items()) if v == (project_ns, role_name)]:
    self._pane_tokens.pop(_t, None)      # revoke old FIRST
self._pane_tokens[_tok] = (project_ns, role_name)   # then register new
```
- shell `1448` · gemini `1532` · codex `1593` · claude `1813` (else-branch, Lead ใช้ `_lead_token` แยก) — **ครบ 4 respawn branch**.

**revoke ครบทุก exit path:**
| path | จุด | สถานะ |
|---|---|---|
| spawn fail (except) | shell/gemini/codex `pop(_tok)` ; claude `2121` `if role!=LEAD: pop(pane_tok)` | ✅ (claude guard กัน NameError ตอน Lead) |
| session crash/exit | `_on_session_exit` `2244` revoke **ก่อน** early-return guard (`pane.state!="exited"`) | ✅ revoke ถึงแม้ bookkeeping return |
| close() | `3400` revoke loop | ✅ |
| respawn | revoke-before-register (ข้างบน) | ✅ |

**stale-exit ไม่ revoke ผิดตัว:** connect-site guard `pp.session is s` (shell/gemini/claude) ทำให้
stale exit จาก session เก่า **ไม่เรียก `_on_session_exit`** → ไม่ revoke token ของ session ใหม่ —
ถูกต้อง (token เก่าถูก revoke ไปแล้วตอน respawn). ✅

**test:** `TestTokenRevocationOnSessionExit` เรียก `Orchestrator._on_session_exit` ตัวจริง +
`srv._dispatch` ด้วย revoked/old/new token → reject/accept ถูกต้อง (4 case). ✅

**residual area1:** เหลือเฉพาะ window inherent (token valid ตั้งแต่ process ตายจริง →
ถึง `processExited` ถูก process บน main thread). Loopback-only + leaked-secret เท่านั้นที่ใช้ได้ →
**ยอมรับได้ ไม่ใช่ blocking.** `_lead_token` ไม่เคย revoke (long-lived per-orchestrator) = by design.

---

## (2) TCP — ✅ PASS

**`_open_connections.pop` fix ถูกต้อง (track until disconnected):**
- pop **เฉพาะตอน `disconnected` signal** (`_on_new_connection`): `sock.disconnected.connect(lambda s: self._open_connections.pop(s, None))`.
- valid frame → `self._open_connections[sock] = time.time()` (update last-activity, **ไม่ pop**) → connection ยังนับใน `_MAX_CONNECTIONS` ทั้ง lifetime + reaper เห็น. **fix round1 bypass (pop-after-first-frame → untracked) เรียบร้อย.** ✅

**unterminated / oversized frame — no memory blowup เหลือ:**
- pre-check: `bytesAvailable() > _MAX_FRAME_BYTES and not canReadLine()` → reject+disconnect+pop. กัน flood ไม่มี `\n`. ✅
- bounded read: `readLine(_MAX_FRAME_BYTES + 2)` + `if len(raw) > _MAX_FRAME_BYTES: reject` → Qt ไม่ buffer line ยักษ์. ✅
- slow-drip < MAX ไม่มี newline → loop ไม่ทำงาน, timestamp ไม่ refresh → **reaper เก็บใน 30s**. ✅
- connection cap: reject ที่ `_on_new_connection` ถ้า `>= 32`. bounded 32 concurrent + churn. ✅

**reaper ปิด conn ค้างถูกจังหวะ:** ใช้ last-activity ts (refresh เฉพาะ valid frame; empty-line/newline-flood
**ไม่** refresh → ยังโดน reap). ✅

**schema hardening:** `isinstance(req, dict)` + type-check `cmd/from/auth` ต้องเป็น str ก่อน `_dispatch`
(กัน `[]`/`null`/scalar/non-str raise นอก try). ✅

**test:** `TestTcpConnectionTracking` ยิง `srv._on_ready_read` / `_reap_idle_connections` ตัวจริง —
unterminated-oversized-reject, remains-tracked-after-frame, last-activity-refresh, idle-reaped (4 case). ✅
`TestFrameValidation` คุม type/dict. (imports `secrets`/`time` ครบ `cli_server:16-17`.)

---

## (3) Codex stale-session guard — ◐ PASS (มี LOW residual)

**guard เพิ่มถูกที่ (ก่อน `_on_session_exit` + crash-dump):** `_on_codex_exit:2151-2155`
```python
_pane_cdx = self._panes_by_project.get(project, {}).get(role_name)
if _pane_cdx is not None and _pane_cdx.session is not session:
    return
```
ป้องกัน blocking finding ของ codex (stale codex exit รัน generic exit handling / crash-dump ผิด session). ✅
codex connect-lambda เรียก `_on_codex_exit` **แบบไม่มี inline guard** (จงใจ — ต้องเข้า crash-dump logic);
guard อยู่ในตัว method แทน. **test `TestCodexExitStaleSessionGuard` เรียก `Orchestrator._on_codex_exit` ตัวจริง** (current fires / stale drops / closed drops). ✅

### 🟡 LOW R3a — guard วางหลัง `codex_spawn_ts` reset → clobber live session's spawn_ts
`_on_codex_exit:2144-2148` รัน **ก่อน** guard:
```python
_ps_cx = self._pane_state.get(ekey)
spawn_ts = _ps_cx.codex_spawn_ts if _ps_cx is not None else None
if _ps_cx is not None:
    _ps_cx.codex_spawn_ts = None      # ← มาก่อน guard (2151)
```
`codex_spawn_ts` key ด้วย `(project, role)` = **shared ข้าม respawn**. stale exit จาก session เก่า
จะ set `codex_spawn_ts = None` ของ **session ใหม่ที่ยัง live** ก่อนจะ `return` ที่ guard.
- **ผลกระทบ:** ถ้า session ใหม่ early-crash ตามมา → `time_to_exit = None` → **ไม่เขียน crash dump** (เสีย diagnostic เฉยๆ).
- **ไม่ใช่ lifecycle/security bug:** guard ยังกัน `_on_session_exit` + crash-dump-ผิด-session ได้.
- **reachability แคบ:** respawn มี delay 2s (`singleShot(2_000, _do_respawn)`) → ปกติ old exit ถูก process ก่อน respawn; race นี้ narrow.
- **แก้:** ย้าย guard ขึ้นบนสุดของ method (ก่อนอ่าน `_ps_cx`). untested (test ใช้ `_pane_state={}` จึงไม่โดน path นี้).

---

## (4) Delayed-Enter TOCTOU — ◐ PASS (มี LOW residual)

**helper `_delayed_enter` ปลอดภัยจาก TOCTOU (3 ชั้น):**
```python
def _delayed_enter(pane, session, delay_ms):
    QTimer.singleShot(delay_ms, lambda: pane.session is session and pane.session.write(b"\r"))
```
1. capture `session` ตอน paste (ไม่ deref slot ตอน fire)
2. fire-time guard `pane.session is session` → pane respawn แล้ว session ใหม่ ≠ → **no-op** (กัน `\r` ลง session ใหม่)
3. `PtySession.write` guard `if not self._alive: return` (`pty_session:384`) → same-but-dead session ก็ no-op

**ครอบทุก hot path (capture session ก่อน paste):** auto-slash `2513` · task-inject `2584`
(+`_sanitize_pane_text`) · notify-lead pump `3223` · peer send `3294` · provider-toggle notice `3476` ·
tier notice `3529` · done-close `3691` (`_close_if_same_session` capture `_done_sess`). ✅
`_flush_pending_lead_cc:2791` capture `s=lead.session` by-value (ปลอดภัยผ่าน write `_alive` guard
แต่ไม่ใช้ helper — inconsistent, harmless).

**test:** `TestDoneCloseSessionGuard` (done-close) ยิง path จริง. ✅

### 🟡 LOW R4a — `orchestrator.py:4578-4580` harvest-hint ยังไม่แปลงเป็น `_delayed_enter`
```python
lead_pane.session.write(hint_msg)
QTimer.singleShot(150, lambda lp=lead_pane: lp.session and lp.session.write(b"\r"))
```
deref `lp.session` (mutable pane slot) ตอน fire → ถ้า **Lead pane respawn ภายใน 150ms** →
`\r` ลง Lead session ใหม่ (submit บรรทัดว่าง). **M3 pattern ที่ยังไม่ถูกกำจัดหมด.**
- **LOW:** window 150ms, Lead respawn หายาก, ผลแค่ blank-line submit, มี `lp.session and` กัน crash.
- **แก้:** `_sess = lead_pane.session; _sess.write(hint_msg); _delayed_enter(lead_pane, _sess, 150)`.
- codex round1 list สาย delayed-Enter หลายจุด (รวมโซน ~4876) — จุดนี้คือที่ยังหลุด.

---

## Residual summary (ไม่มี blocking)

| ID | finding | severity | จุด | แก้ |
|---|---|---|---|---|
| R4a | harvest-hint delayed-CR ยัง deref `pane.session` ตอน fire (M3 ยังไม่หมด) | 🟡 LOW | orch `4578-4580` | capture session → `_delayed_enter(lead_pane, _sess, 150)` |
| R3a | codex guard อยู่หลัง `codex_spawn_ts=None` → clobber spawn_ts ของ live session (เสีย crash-dump) | 🟡 LOW | orch `2144-2155` | ย้าย guard ขึ้นบนสุด method ก่อนอ่าน `_ps_cx` |
| info | `_flush_pending_lead_cc:2791` ไม่ใช้ helper (by-value capture, safe) | ℹ️ INFO | orch `2789-2792` | (optional) ใช้ `_delayed_enter` เพื่อ consistency |

---

## ✅ ยืนยันผ่าน (ตามที่ task โฟกัส)
- **register-new-after-revoke-old ordering ถูกต้องทั้ง 4 branch** — ไม่มี window token ซ้อน
- **token revoke ครบ exit/close/spawn-fail/respawn** — `_on_session_exit` revoke ก่อน early-return guard
- **`_open_connections` track until disconnected** — fix round1 bypass สมบูรณ์; reaper + cap + bounded readLine + bytesAvailable ครบ
- **codex guard drop stale ก่อน `_on_session_exit`/crash-dump** — กัน detach/respawn ผิด session
- **`_delayed_enter` capture session + 2 guard (is/alive)** — TOCTOU paste→Enter ปิดในทุก hot path
- **test ยิง production path จริง** (`_on_codex_exit`/`_on_session_exit`/`_on_ready_read`/`_reap_idle_connections`/`_dispatch`) — แก้ test-quality gap ที่ codex round1 ตำหนิ

## Test verification
```
python -m pytest tests/                                  → 1947 passed, 2 skipped (47.4s)
tests/test_regression_findings_2026_06.py                → 43 passed
auth + role_gate + end_session + codex_crash             → 75 passed
```
ไม่มี failure ใน critical path · 0 regression.
