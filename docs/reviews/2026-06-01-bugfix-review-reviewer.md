# Code Review — bug-fix batch (gap-audit) · 2026-06-01

> Reviewer: code-reviewer agent · scope: uncommitted diff (`git diff` + ไฟล์ใหม่ `??`)
> Snyk: **N/A** — `snyk` ไม่ได้ติดตั้งใน PATH (`snyk: command not found`). โปรเจกต์เป็น pure-Python desktop (PyQt6) ไม่มี npm/lockfile-based dependency surface ที่ critical — manual review เป็นหลัก
> Tests: **180 passed** (test_lifecycle_recovery, test_project_rules, test_provider_substitution_note, test_routing_planner, test_orchestrator_auto_respawn_replay, test_stuck_recover, test_teammate_tier)

## TL;DR

**ไม่พบ blocker.** ตรรกะ lifecycle fix (Bug 1/3/4/6/7) ถูกต้องและ key-format สอดคล้องกันทั้งหมด, provider-truth logic ถูก, routing impl-gate ทำงานตามเจตนา (verified empirically). มี **1 MAJOR** (Qt signal shadowing — ทำงานได้แต่เป็น footgun) และชุด **MINOR** ที่เป็น fragility/edge-case ส่วนใหญ่แก้ทีหลังได้

---

## Findings

### 🔴 Blocker
ไม่มี

### 🟠 Major

**M1 — `_RulesGeneratorThread` redefine signal ชื่อ `finished` ทับ built-in ของ QThread**
`src/agent_takkub/main_window.py:115`
```python
class _RulesGeneratorThread(QThread):
    finished: pyqtSignal = pyqtSignal(str)   # ← ทับ QThread.finished()
    failed: pyqtSignal = pyqtSignal(str)
```
QThread มี signal `finished()` ในตัวอยู่แล้ว (emit เมื่อ `run()` จบ). การประกาศ `finished = pyqtSignal(str)` ทับมัน → `thread.finished.connect(...)` ทุกที่จะผูกกับ signal เวอร์ชัน custom (str) แทน lifecycle signal ของ Qt.
- **ตอนนี้ยังไม่พัง** เพราะ `_generate_rules_with_ui()` ใช้ `thread.wait(...)` คุม lifecycle แทน (main_window.py:2775) ไม่ได้พึ่ง `QThread.finished` semantics
- **ความเสี่ยง:** ใครก็ตามที่เพิ่ม `thread.finished.connect(thread.deleteLater)` ในอนาคต (idiom มาตรฐาน) จะได้พฤติกรรมผิด — fire ตอน custom emit + ส่ง str เข้า deleteLater. เป็น anti-pattern ที่ Qt docs เตือนชัดเจน
- **Fix:** rename → `rulesReady = pyqtSignal(str)` (เลี่ยงชื่อ `finished`/`started` ที่ base class จองไว้)

### 🟡 Minor

**m1 — Bug-2 content-delta: filter `"esc to interrupt"` เป็น string-coupling เปราะกับเวอร์ชัน CLI**
`src/agent_takkub/orchestrator.py:3283-3285`
```python
non_spinner_hash = str(hash(tuple(
    ln for ln in disp if "esc to interrupt" not in ln.lower()
)))
```
สมมติว่า "บรรทัด spinner ทั้งหมดมี substring 'esc to interrupt'". ถ้า claude CLI เวอร์ชันใด render บรรทัด counter แยก (เช่น `· 45s · ↑ 2.3k tokens`) ที่ **ไม่มี** "esc to interrupt" → บรรทัดนั้นเปลี่ยนทุกวินาที → content-hash เปลี่ยนตลอด → **stuck ไม่ถูกตรวจจับเลย** (Bug-2 fix กลายเป็น no-op เงียบๆ). กลับด้าน: งาน legit ที่ใช้เวลานานเกิน `STUCK_THRESHOLD_S` (10 นาที) บน single tool call ที่ไม่ขยับจอ → โดน force-recover (false positive) แต่ 10 นาที conservative พอจน rare. **แนะนำ:** กรอง spinner region แบบทนทานกว่า (จับ pattern เวลา/token counter เพิ่ม) หรืออย่างน้อยเขียน test เทียบกับ banner จริงของ CLI เวอร์ชันปัจจุบัน

**m2 — Bug-5 re-paste gate `"(resumed)" in msg` เป็น string coupling**
`src/agent_takkub/orchestrator.py:1663` และ `:3366`
```python
spawn_resumed = "(resumed)" in msg     # 1663
... if ok and snap_task and "(resumed)" not in msg:   # 3366
```
gate การ re-paste task อาศัย substring ใน human message ที่ `spawn()` คืน (`suffix = " (resumed)"` ที่ orchestrator.py:1496). ถ้าถ้อยคำ return message เปลี่ยน → gate พังเงียบ: ไม่ resume แต่ไม่ re-paste = **เสียงาน**, หรือ resume แล้ว re-paste = **duplicate work บน step ที่ไม่ idempotent** (file create/migration) ซึ่งตรงข้ามกับสิ่งที่ Bug-5 พยายามกัน. **แนะนำ:** ให้ `spawn()` คืน `resumed: bool` แบบ structured (เช่น 3-tuple หรือ attribute) แทนการ parse string. ตอนนี้ทำงานถูกเพราะ `(resumed)` โผล่เฉพาะตอน resume จริง และ path/role ไม่มีทางมี substring นี้

**m3 — Bug-1 restore พึ่ง timing: `_recent_exits` ต้องถูกบันทึกภายในหน้าต่าง 2s**
`src/agent_takkub/orchestrator.py:3346-3372`
`close()` → `terminate()` (async) → `processExited` → `_on_session_exit` บันทึก `_recent_exits` (orchestrator.py:1610). `_do_respawn` รันหลัง `QTimer.singleShot(2_000, ...)`. `can_resume` (line 1438) ต้องการ `prior_exit is not None` ภายใน `RESUME_WINDOW_SEC`. ถ้า PTY teardown ช้ากว่า 2s (เครื่องช้า/winpty หน่วง) → `_recent_exits` ยังไม่มี entry ตอน `_do_respawn` → `can_resume=False` → spawn blank session = **เคสที่ Bug-1 ตั้งใจกันพอดี**. ปกติ 2s พอ, แต่เป็น race ที่ไม่มี guard. **แนะนำ:** poll การมีอยู่ของ recent-exit ก่อน respawn หรือเพิ่ม margin. *(หมายเหตุเชิงบวก: snapshot/restore ของ 4 field ทำ **ก่อน** `close()` และ restore **ก่อน** `spawn()` ครบถูกต้อง — key format `f"{project}::{role}"` ตรงกับ `_exit_key()` ทุกจุด, ยืนยันแล้ว)*

**m4 — `generate_project_rules_proc` ไม่กัน `find_claude_executable()` คืน None**
`src/agent_takkub/project_rules.py:45,52`
```python
claude = find_claude_executable()       # อาจคืน None
return subprocess.Popen([claude, "-p", ...])   # None → TypeError
```
ถ้าหา claude binary ไม่เจอ → `Popen([None,...])` โยน `TypeError` (ไม่ใช่ RuntimeError ที่ caller รอจับ). ความน่าจะเป็นต่ำ (claude เป็น baseline ของ cockpit) แต่ควร raise RuntimeError ชัดเจน

**m5 — Save editor ว่าง → เขียน CLAUDE.md ว่าง (data loss)**
`src/agent_takkub/main_window.py:2984-2986`, `:2939`, `:2664`
`do_save()` set `outcome[0] = editor.toPlainText()` โดยไม่เช็ค non-empty. string `""` ผ่าน `isinstance(result, str)` / ผ่าน `else` → `write_project_rules(root, "")` เขียนทับ CLAUDE.md เดิมเป็นไฟล์ว่าง. **แนะนำ:** เตือน/บล็อกถ้า strip แล้วว่าง

**m6 — `_save_and_open_project` ทับ description/presets ของ project เดิมเมื่อชื่อซ้ำ**
`src/agent_takkub/main_window.py:2874`
```python
data["projects"][name] = {"description": name, "paths": paths, "presets": []}
```
re-add ชื่อเดิม → ล้าง `presets`/`description` ที่ user ตั้งไว้ (มี warning duplicate เฉพาะเคส folder ต่าง). minor data loss

**m7 — `_RulesGeneratorThread` parented to `self` ไม่เคย `deleteLater()`**
`src/agent_takkub/main_window.py:2750` — thread object ค้างเป็น child ของ MainWindow ทุกครั้งที่ generate. สะสมช้าๆ (1/ครั้ง) ไม่รุนแรง แต่ควร cleanup หลัง `wait()`

### 🔵 Notes (ไม่ใช่ bug — บันทึกไว้)

- **N1 — `quit()` บน reader/writer thread เป็น dead call** (`pty_session.py:349,352`): สอง thread รัน `while`-loop ธรรมดา ไม่มี `exec()` event loop → `quit()` ไม่มีผล. การหยุดจริงมาจาก `request_stop()` (sentinel/flag) + `proc.terminate`. `wait(500)` คือตัว join จริง. ไม่อันตราย แต่ทำให้อ่านเหมือนมี event loop. **Bug-7 ยืนยัน: ไม่มี deadlock risk** — `terminate()` ถูกเรียกจาก UI thread เสมอ (close→terminate), reader/writer เป็นคนละ thread, `finished_clean` เป็น queued connection → ไม่มี self-join
- **N2 — `_IMPLEMENTATION_TH = (ทำ|สร้าง|เพิ่ม|เขียน|จัด)`** (`routing_planner.py:60`): `ทำ`/`จัด` แบบเปลือยจับ `ทำงาน`/`ทำไม`/`จัดการ` ได้ (ต่างจาก `_ACTIONABLE_TH` ที่มี negative-lookahead). **แต่ verify แล้วว่าไม่ false-positive จริง** — `"ทำไม UI กับ API..."` → `INFORMATIONAL` เพราะ informational gate ตัดก่อนถึง multi-role branch. ความเสี่ยงต่ำ; ถ้าจะ harden ให้ใช้ lookahead แบบเดียวกับ `_ACTIONABLE_TH`
- **N3 — token:** lead_context inject project CLAUDE.md (≤3000 ตัวอักษร) ทุก Lead spawn สำหรับ non-cockpit project + provider section. เป็น feature ตั้งใจ + user อยู่บน Max (token cost ไม่ใช่ข้อกังวลตาม project policy). provider section suppress เมื่อทุก provider พร้อม (ประหยัด token เคสปกติ) — ดี
- **N4 — provider-truth logic ถูกต้อง** (`lead_context.py`): `if _is_disabled → toggled_off; elif not _check_available → not_installed`. `_provider_available` คืน False ทั้งสองกรณีแต่ `elif` กันไม่ให้ double-count, precedence toggle > install ถูกต้อง
- **N5 — Bug-3/Bug-4/Bug-6 ยืนยันถูกต้อง:** `_warn_lead_respawn_capped` guard `lead.session.is_alive` + ล้าง `_auto_chain_panes`/`_last_assigned_task`; `_from_auto_respawn` gate (manual=reset counter, auto=keep) สอดคล้องกับ assign path; `close()` pop ครบ 5 dict ใหม่ (`_harvest_hint_ts`/`_last_stuck_recover`/`_rate_limited_until`/`_last_content_hash`/`_last_content_change_ts`). ไม่มี double-respawn เพราะ `mark_expected_exit()`→state `"empty"`→`_on_session_exit` guard `state != "exited"`

---

## สรุป severity
| ระดับ | จำนวน |
|---|---|
| Blocker | 0 |
| Major | 1 (M1 Qt signal shadow) |
| Minor | 7 (m1–m7) |
| Notes | 5 |

**คำแนะนำ ship:** M1 ควรแก้ก่อน merge (rename signal, 1 บรรทัด). m1/m2 (string coupling) ควรขึ้น follow-up issue เพราะกระทบ correctness ของ recovery เมื่อ CLI/message format เปลี่ยน. m3–m7 แก้ทีหลังได้
