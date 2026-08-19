# PR #311 review — Core V2 ขั้น 1–9 (feat/v2-core → main)

**Reviewer**: reviewer pane · **Scope**: trust-boundary/regression risk only (ไม่รีวิว style) · **Date**: 2026-08-19

**Verdict: MERGE-WITH-FIX**

ไม่มี correctness/security bug ที่ block ตรงๆ (flag-off outcome เหมือนเดิมจริงทุกจุดที่เช็ค, copy-never-move ยึดจริง, `migrate apply` เรียกไม่ได้โดยบังเอิญจริง) แต่เจอ 1 finding ที่ตรงกับสิ่งที่ PR เคลมไว้ในเอกสารเอง (docstring อ้างว่า "flag OFF = zero disk I/O") ซึ่งไม่จริง — เป็น regression บน Qt main thread ที่วัดจากโค้ดจริงได้ ไม่ใช่ทฤษฎี — ควรแก้ก่อน merge หรืออย่างน้อยก่อน flip flag ตัวไหนก็ตามใน production

---

## Must-fix

### 1. ทุก `v2_*_enabled()` อ่านดิสก์ทุกครั้งที่เรียก เมื่อ env var ไม่ถูกตั้ง (ค่า default จริงของทุกเครื่องวันนี้) — ขัดกับ docstring ของ PR เองที่เคลมว่า "flag off = zero disk I/O" และขัดกับข้อกำหนดข้อ 1 ของรีวิวนี้ ("ไม่ทำ I/O บน Qt main thread")

**หลักฐาน:**
- `src/agent_takkub/core_v2_settings.py:93-94` — `flag_enabled()` เรียก `load()` ตรงๆ ไม่มี cache เลย (ต่างจาก `core/scheduling/facade.py:effective_slot_policy()` ที่ cache ด้วย mtime)
- `src/agent_takkub/core_v2_settings.py:65-70` — `load()` ทำ `target.read_text(encoding="utf-8")` + `json.loads()` แบบ synchronous ทุกครั้ง
- ทุก flag module (`core/routing/flag.py`, `core/conversation/flag.py`, `core/brain/flag.py:_env_or_setting`, `core/scheduling/flag.py`) ใช้ pattern เดียวกัน: `os.environ.get(NAME)` → ถ้า `None` (ไม่ตั้ง env — ค่า default ของทุกเครื่องที่ยังไม่เคยแตะ Settings UI) → fallback ไป `core_v2_settings.flag_enabled(...)` ซึ่งอ่านดิสก์เสมอ
- `src/agent_takkub/core/scheduling/facade.py:65-94` — `extended_denial_reason` / `backpressure_level` / `backpressure_admits` **แต่ละตัว**เรียก `v2_scheduler_enabled()` เอง (3 ครั้งต่อ 1 call ของ `_denial_reason`) → 3 disk read ต่อครั้ง ไม่ใช่ 1
- `src/agent_takkub/resource_governor.py:184-193` — `request_slot()` เรียก `_denial_reason()` ทุกครั้งที่มี pane ขอ slot (ทุก spawn/dispatch) — **ก่อน PR นี้** resource class LIGHT/NORMAL คืน `""` ทันทีไม่มี I/O เลย (`resource_governor.py` เก่า บรรทัด 94-95 เดิม) — **หลัง PR นี้** ทุก class (รวม LIGHT/NORMAL) ต้องวิ่งผ่าน backpressure check 3 จุดก่อนเสมอ
- `src/agent_takkub/orchestrator.py:1076-1079` — `self._resource_timer = QTimer(self); setInterval(1000); timeout.connect(self._tick_resource_governor); start()` → ยิงทุก 1 วินาทีบน **Qt main thread** (ยืนยันจาก comment บรรทัด 1064-1066: "dispatch callbacks run on this Qt thread") เรียก `governor.dispatch_waiting()` ซึ่งถ้ามี waiting task อยู่จริง (เคสปกติเวลามี fan-out หลาย pane รอ slot) จะวน `request_slot()` ต่อ item — ทวีคูณ I/O ต่อ tick
- `orchestrator.py`'s `_inject_v2_context` (บรรทัด ~816-820, `v2_context_enabled()`), `done()`/`_subagent_done` hook (`v2_conversation_enabled()`, `v2_brain_enabled()`) — เช็ค flag แบบ inline **ก่อน** spawn thread เสมอ (ตามที่ตั้งใจไว้ว่า I/O ต้องอยู่ใน background thread) แต่ตัว flag-check เองก็เป็น disk I/O ที่ยังคงรันบน Qt main thread อยู่ดี เพราะเช็คก่อนตัดสินใจว่าจะ spawn thread หรือไม่
- `spawn_engine.py:1890-1894, 2389-2393` — `v2_router_enabled()` ถูกเช็คตรงๆ ทุก spawn (ไม่ threaded)

**ทำไมสำคัญ:** flag ทั้ง 5 ตัวยังไม่มีใครเปิดใน production (`TAKKUB_V2_*` ทุกตัว False by default) — นั่นแปลว่า "ค่า default" ของทุกเครื่องคือ "env unset" ซึ่งเป็น path ที่แพงที่สุด ไม่ใช่ path ที่ถูกที่สุดตามที่เอกสารเคลม ผลคือ **ทุกเครื่องที่รัน PR นี้วันแรกที่ merge** (ไม่ต้องเปิด flag อะไรเลย) จะเห็น disk read เพิ่มขึ้นจริงบน Qt main thread ทุกวินาที + ทุก spawn — ตรงข้ามกับ "flag off = พฤติกรรมเดิมเป๊ะ" ที่เป็น hard rule ของ epic นี้เอง (เอกสาร plan §0 rule 3, และข้อกำหนดข้อ 1 ของ task นี้)

**ระดับผลกระทบ (ตามจริง ไม่ใช่ทฤษฎี):** ไฟล์เล็ก (< 1KB JSON), OS page cache ทำให้ syscall จริงเบา — นี่ไม่ใช่บั๊กที่ทำให้ UI ค้างเห็นได้ชัด แต่เป็น "unconditional disk I/O บน UI thread ทุก 1 วินาที" ซึ่งเป็นสิ่งที่ guardrail ของโปรเจกต์นี้เคย incident จริงมาแล้ว (`_graft_progress_snapshot` เคสก่อนหน้า) และขัดกับเอกสารของ PR เองแบบตรงไปตรงมา — ไม่ใช่ edge case ต้องตีความ

**ทำไม test ไม่จับ:** `tests/test_resource_governor_scheduling.py::test_flag_off_slot_policy_is_ignored` (บรรทัด 46-47) และ `test_flag_off_dispatch_waiting_keeps_fifo_project_order` (บรรทัด 212-213) ทำ `monkeypatch.delenv("TAKKUB_V2_SCHEDULER", raising=False)` ซึ่งจำลอง "env unset" ได้ถูกต้อง — **แต่ทดสอบแค่ outcome (denial reason/order เหมือนเดิม)** ไม่ได้ assert จำนวนครั้งที่ `path()`/`load()`/`read_text()` ถูกเรียก จึงเป็น false-negative ต่อ I/O regression แบบนี้โดยธรรมชาติ — ไม่ใช่ test เขียนผิด แค่ไม่ได้ออกแบบมาจับมิตินี้ (`conftest.py`'s ตัว redirect `core_v2_settings.path()` ก็ยังคงอ่านดิสก์จริงที่ tmp path เดิม ไม่ได้ mock ออก)

**แนะนำ:** cache ผลลัพธ์ของ `core_v2_settings.load()` ด้วย mtime (pattern เดียวกับที่ `core/scheduling/facade.py:effective_slot_policy()` ทำอยู่แล้วในไฟล์เดียวกัน — ใช้ pattern เดิมได้เลย ไม่ต้องคิดใหม่) แล้วให้ `flag_enabled()` ทั้ง 5 ตัวใช้ cache เดียวกัน จะตัด I/O ต่อ tick ให้เหลือแค่ 1 `stat()` เทียบ mtime แทนที่จะเป็น `read_text()+json.loads()` เต็มทุกครั้ง — และยังปิด gap ที่ `_denial_reason` เรียก `v2_scheduler_enabled()` ซ้ำ 3 รอบต่อ 1 call ไปในตัว

---

## ยืนยันแล้วว่าไม่มีปัญหา (ตรวจแล้ว ไม่ใช่แค่เชื่อ docstring)

**1) flag-off byte-identical (นอกเหนือจาก I/O ด้านบน):**
- `core/routing/facade.py:effective_provider_for_v2` — off เรียก `provider_config.effective_provider_for` ตรงๆ, fail-open ครบ ✓
- `resource_governor.py:_denial_reason` — เดิน logic ใหม่ผ่าน `_denial_reason` ตรวจ code path ทีละบรรทัดแล้ว: ผลลัพธ์ (denial reason string) เหมือนเดิม 100% เมื่อ flag off เพราะ facade ทุกฟังก์ชันเช็ค `v2_scheduler_enabled()` เองก่อน return no-op value — เปลี่ยนแค่ "เดินทางไกลขึ้น" ไม่เปลี่ยนผลลัพธ์ ✓ (แต่ดู must-fix ข้อ 1 เรื่อง I/O)
- `orchestrator.py:_inject_v2_context` — off คืน `task` เดิมก่อน import อะไรเลย (`if not v2_context_enabled(): return task`) ✓
- `orchestrator.py` conversation/brain hooks ใน `done()`/`_subagent_done` — off short-circuit ก่อน import, on ทำงานใน background thread จริง (`threading.Thread(..., daemon=True)`) ไม่ block caller ✓
- context-builder 300ms timeout ใน `_inject_v2_context` ใช้ `ThreadPoolExecutor(max_workers=1)` + `future.result(timeout=0.3)` + `executor.shutdown(wait=False)` — worker ที่ timeout แล้วจะไม่ block การ return, ตรวจแล้วไม่มี thread leak ที่กระทบ correctness (แค่กิน thread ชั่วคราวจนงานเก่าจบเอง) ✓

**2) secret rung — 1 gap สำคัญที่ควรรู้แต่ไม่ block:**
- `core/secrets/redact.py:redact()` — regex ครอบคลุมดี (sk-ant-, sk-generic, gh token, Bearer, JSON cred field) **แต่ไม่มีจุดไหนใน production code เรียกใช้เลย** (`grep -rn "redact(" src/agent_takkub` เจอแค่ตัวมันเอง + `auto_issue_capture.py` ที่มี `_redact` แยกต่างหากของตัวเอง ไม่เกี่ยวกัน)
- ผลคือ `core/conversation/store.py:append_message` (บรรทัด 124-151) เขียน `text` (raw note/task จาก pane) ลง `messages.jsonl` **โดยไม่ผ่าน redact เลย** — ถ้า teammate เขียน note ที่แปะ error log/env dump ที่มี token/credential ติดมา (เคสสมจริง — เกิดขึ้นได้เวลา debug auth failure) จะถูกเก็บถาวรลง jsonl แบบ plaintext
- ไม่ block เพราะ `TAKKUB_V2_CONVERSATION` ปิดอยู่ default และนี่คือ "ยังไม่เชื่อม" ไม่ใช่ "เชื่อมผิด" — แต่เป็น gap ที่ไม่ถูกประกาศไว้ใน PR body's "Gap ที่ประกาศ" list เลย ควรเพิ่มเข้า tracked-gap ก่อน flip `TAKKUB_V2_CONVERSATION` จริง ไม่งั้นจะลืม

**3) account env override (`spawn_engine.py:_apply_v2_account_env_override`, `core/accounts/facade.py:resolve_account_for`):**
- วันนี้ `AccountPoolRegistry().all()` ไม่มีใคร populate (ยืนยันจาก `core/accounts/facade.py`'s docstring + ไม่มี caller อื่นเรียก write เข้า pool registry) → ทุก resolve จะ fall through ไป legacy path (`read_selected_account_id` + `read_legacy_accounts`) เสมอ ซึ่งอ่านจากไฟล์เดียวกับที่ `pane_env.inject_user_profile_env`/`inject_provider_home_env` (legacy) อ่านอยู่แล้ว → `config_dir` ที่ได้ตรงกับของเดิม → `account_env_overrides` (`core/providers/plan.py:47-56`) set `CLAUDE_CONFIG_DIR`/`CODEX_HOME` เป็นค่าเดิมที่ legacy injector set ไปแล้ว = no-op ในทางปฏิบัติ ✓ ยืนยันตรงกับที่ docstring เคลม
- exception path (`_log.exception`) ไม่ log ค่า `env`/secret เอง log แค่ provider_id/role_name — ไม่มี leak เข้า log ✓

**4) `core/storage` + `core/migration` — copy-never-move:**
- `grep -n "shutil.move|.unlink(|os.remove|os.rename|.rename(" src/agent_takkub/core/migration/*.py` → **0 match** ทุกไฟล์ (`steps.py`, `steps_v1.py` 788 บรรทัด, `registry_copy_step.py`, `backup.py`, `journal.py`) — ไม่มี destructive op เลยในทั้ง migration ladder ✓
- `takkub migrate apply` เรียกไม่ได้โดยบังเอิญจริง: `cli.py:1501-1517` (`cmd_migrate`) เป็นทางเดียวที่ dispatch ไป `engine.apply` และต้องพิมพ์ `takkub migrate apply` ตรงๆ ผ่าน CLI subparser (`migrate_cmd` required) — ฝั่ง UI (`settings_core_v2.py:_MigrationReportThread`, บรรทัด 148-161) จงใจตัด `"apply"` ออกจาก dispatch dict เหลือแค่ `inspect/plan/dry_run` ตรงกับ comment ที่บรรทัด 1100 ("apply ทำผ่าน CLI เท่านั้น ไม่มีปุ่มนี้ใน UI") ✓ grep หา caller อื่นของ `MigrationEngine()`/`.apply()` ทั่ว repo เจอแค่ 2 จุดนี้ ไม่มีจุดที่ 3 ✓

**5) `conftest.py` redirect `core_v2_settings.path()`:**
- Fixture ใหม่ (`_isolate_runtime`, บรรทัด ~205-212) ทำ `monkeypatch.setattr(cvs, "path", lambda: _v2_path, raising=False)` บน module object ที่ import ผ่าน `_maybe_module(..., force=True)` (คืน module เดียวกับที่อยู่ใน `sys.modules` หรือ import ใหม่ครั้งแรก ไม่ใช่ reload) — grep ทั้ง repo ไม่มีที่ไหนทำ `from ...core_v2_settings import path` ตรงๆ (ทุกจุดเรียกผ่าน `core_v2_settings.path()`/`.flag_enabled()` module-qualified) จึง monkeypatch มีผลครบทุก call site จริง ไม่มี stale reference ✓ ไม่กระทบ test อื่น

**6) เอกสารไม่ตรงเล็กน้อย (ไม่ block):** PR body table อ้างอิง `docs/v2/phase1-report.md` และ `phase2-report.md` แต่ทั้งสองไฟล์ไม่เคยถูกสร้างเลยตลอด branch นี้ (`git log --diff-filter=A -- docs/v2/*` ไม่มี commit ไหนเพิ่มไฟล์เหล่านี้) — ลิงก์ตายในคำอธิบาย PR เฉยๆ ไม่กระทบโค้ด

---

## สรุป

| จุดตรวจ | ผล |
|---|---|
| flag gate มีครบทุกจุดเชื่อม | ✓ มีครบ |
| fail-open ทุกจุด | ✓ ยืนยันแล้ว (try/except ครอบทุก hook, ทุก facade function) |
| ไม่ทำ I/O บน Qt main thread เมื่อ flag off | ✗ **ไม่จริง** — ดู must-fix #1 |
| secret ไม่รั่วเข้า log/events | ✓ ไม่รั่วเข้า log แต่ ⚠ รั่วเข้า jsonl ได้ (ดู #2, ไม่ block) |
| account env override ผิด account | ✓ ไม่มีปัญหา (no-op ในทางปฏิบัติวันนี้) |
| migration apply เรียกพลาดได้ | ✓ เรียกไม่ได้โดยบังเอิญ |
| copy-never-move | ✓ ยึดจริง ไม่มี destructive op |
| conftest.py redirect กระทบ test อื่น | ✓ ไม่กระทบ |

**ข้อเสนอ:** merge ได้หลังแก้ must-fix #1 (cache `core_v2_settings.load()` ด้วย mtime) — ไฟล์เดียว (`core_v2_settings.py`) แก้จุดเดียวแก้ได้ครบทั้ง 5 flag เพราะทุกตัวเรียกผ่าน `flag_enabled()`/`load()` เดียวกัน ไม่ต้องแตะทั้ง 4 ไฟล์ hot-path ที่ diff มา ผลกระทบต่อ scope การแก้เล็กมากเทียบกับความเสี่ยงที่ตัด ถ้าจะ merge โดยไม่แก้ก่อน ควรอย่างน้อยเปิด issue tracked ไว้ก่อน flag ตัวไหนถูก flip เป็น default-on จริงใน production
