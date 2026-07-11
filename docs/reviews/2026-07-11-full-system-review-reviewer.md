# Full-system code review — agent-takkub

**Reviewer:** reviewer (code review specialist)
**Date:** 2026-07-11
**Scope:** whole `src/agent_takkub/**` (not just latest diff) — correctness, cross-platform (Win+macOS), multi-provider (#103), security, architecture, consistency
**Focus files (newest, highest bug-risk):** `task_ledger.py`, `task_dock.py`, `custom_roles.py`, `settings_window.py`, `cockpit_theme.py`, `lead_draft_state.py` — plus core engine (`orchestrator.py`, `spawn_engine.py`, `lead_inbox.py`, `pipeline_executor.py`)

## สรุปผู้บริหาร (TL;DR)

**ไม่พบ correctness/security blocker.** โค้ดใหม่รอบล่าสุด (ledger, task dock, custom roles, settings window, draft state) เขียนได้สะอาดและ defensive มาก — transactional writes + rollback, atomic file replace, best-effort degradation, cross-platform branch ครบทั้ง 3 OS (win/mac/linux), ไม่มี `shell=True`/`os.system` ที่ไหนเลย.

พบ **1 MED (architecture)** + **2 LOW**. ทั้งหมดเป็น maintainability / edge-case UX ไม่ใช่บั๊กที่ผู้ใช้เจอในเส้นทางปกติ.

| # | Severity | หัวข้อ | ไฟล์ |
|---|---|---|---|
| A1 | **MED** | `orchestrator.py` god-file โตกลับเป็น 4055 LOC (เป้าหลัง refactor 2.7k) | orchestrator.py |
| L1 | LOW | Task-dock card ค้างเมื่อ **ลบ** project ออกจาก projects.json | task_dock.py:352 |
| L2 | LOW | Ledger hook ครอบ `except Exception` กว้าง → บั๊กใน ledger มองไม่เห็นนอก event log | orchestrator.py:905/1829 |

Snyk: ไม่รัน — โปรเจคเป็น pure-Python + PyQt6 ไม่มี npm/requirements lockfile ที่ Snyk ครอบคลุมในโหมด default; dependency audit อยู่นอก scope รอบนี้.

---

## MED

### A1 — `orchestrator.py` โตกลับเป็น 4055 LOC (architecture / maintainability)
**ไฟล์:** `orchestrator.py` (ทั้งไฟล์)

CLAUDE.md บันทึกว่า god-file refactor รอบ 2026-06 ลด `orchestrator.py` จาก 5.8k → **2.7k LOC** โดยแตกเป็น 10 mixins. วันนี้ไฟล์กลับมาที่ **4055 LOC** — โต ~50% เหนือเป้าหลัง refactor และเป็นไฟล์เดียวที่ยังใหญ่กว่าไฟล์อันดับ 2 (`spawn_engine.py` 2048) เกือบเท่าตัว.

**สาเหตุเชิงระบบ:** guardrail ที่มี (import-linter, ตอนนี้ **18 contracts** ขยายจาก 13) บังคับ *ทิศทาง import* / layering ระหว่าง mixin เท่านั้น — **ไม่ได้จำกัดขนาดไฟล์**. ดังนั้น logic ใหม่ (ledger hooks, evidence scan, draft gating, limit/park state ฯลฯ) จึงไหลกลับเข้า `orchestrator.py` ได้เรื่อยๆ โดยไม่ผิด contract ใดๆ แล้วค่อยๆ พอกกลับเป็น monolith เดิม.

**ผลกระทบ:** ไม่ใช่บั๊ก — แต่ทำให้ navigate/review ยากขึ้น (godfile-map.md ต้อง maintain เยอะ), เพิ่มโอกาส merge conflict ตอน parallel fan-out, และลดคุณค่าของ refactor ที่ทำไปแล้ว.

**แนะนำ (ไม่เร่งด่วน):**
- ระบุ cohesive slice ที่ยังอยู่ใน `orchestrator.py` แต่ควรอยู่ mixin แยก — เช่น ledger-hook glue (assign/done/close ทั้ง 3 จุด), evidence-scan (`_scan_done_evidence`), limit-park state — ย้ายออกเป็น mixin ตามแพทเทิร์นเดิม.
- พิจารณาเพิ่ม CI check เตือนเมื่อ `orchestrator.py` (หรือไฟล์ engine ใดๆ) เกิน threshold (เช่น 3000 LOC) — guardrail เชิงขนาดที่ import-linter ให้ไม่ได้ เพื่อกันการพอกกลับเงียบๆ รอบหน้า.

---

## LOW

### L1 — Task-dock card ค้างเมื่อ **ลบ** project (ไม่ใช่แค่ปิด tab)
**ไฟล์:** `task_dock.py:352` `refresh_all()` / `refresh_project()`

`refresh_all()` วน `config.list_project_names()` (= ทุก project ใน projects.json) แล้ว rebuild/remove card เฉพาะ project ที่ยังอยู่ในลิสต์. `refresh_project()` ลบ card เฉพาะเมื่อ project ยังอยู่ในลิสต์แต่ `has_any_rows(state)` เป็น False.

เคสที่ card ค้าง: user **ลบ project ออกจาก projects.json** (ผ่าน wizard remove) ทั้งที่ ledger เคยมี row — project นั้นหลุดจาก `list_project_names()` → `refresh_all` ไม่เคย visit → ไม่มีใครสั่ง `takeTopLevelItem` → **card ค้างในทรีจนกว่าจะ restart app**. `ledgerChanged` ก็ไม่ยิงสำหรับ project ที่ถูกลบ.

*หมายเหตุ:* การ **ปิด tab เฉยๆ ไม่ใช่บั๊ก** — projects.json เก็บ project ไว้ ดังนั้น card ที่ยังโชว์คือ behavior ตั้งใจ (Task List = ประวัติงานข้าม session). บั๊กจริงจำกัดที่ "ลบ project ออกจาก registry".

**แนะนำ:** ใน `refresh_all()` หลังวน `list_project_names()` เสร็จ ลบ top-level card ทุกอันที่ key `project:<name>` ไม่อยู่ในเซ็ต project ปัจจุบัน (reconcile 2 ทาง ไม่ใช่ทางเดียว). Impact เล็ก (แค่รก UI จน restart) จึง LOW.

### L2 — Ledger hook ครอบ `except Exception` กว้างเกิน → บั๊กในตัว ledger มองไม่เห็น
**ไฟล์:** `orchestrator.py:889-906` (assign), `:1822-1830` (done), `:1339-1344` (close)

ทั้ง 3 จุดครอบ `try/except Exception` รอบ `create_assignment`/`mark_done` + `ledgerChanged.emit` แล้ว degrade เป็น `_log_event("ledger_hook_error", ...)` เท่านั้น — เจตนา "ledger เป็น best-effort ห้าม block assign/done จริง" ซึ่ง **ถูกต้องตามดีไซน์**.

ข้อสังเกต: `create_assignment`/`mark_done` เองก็ never-raise อยู่แล้ว (คืน warning string, จับ `OSError` ภายใน) — ดังนั้น `except Exception` ชั้นนอกจะเงียบเฉพาะ *บั๊ก programming จริง* (เช่น `TypeError`/`KeyError` จาก state schema drift). บั๊กแบบนั้นจะหายเข้า event log โดยไม่มีสัญญาณถึง Lead/user เลย — ต่างจาก path อื่นในไฟล์นี้ที่ surface warning ให้ Lead.

**แนะนำ (optional):** ถ้าต้องการ observability เพิ่ม ให้ `_log_event(..., stage=...)` แนบ `exc_info`/`repr(exc)` หรือ `logger.exception(...)` เพื่อให้ traceback ติดใน log — ปัจจุบันเก็บแค่ role/project/stage ทำให้ debug ledger schema drift ยาก. ไม่ใช่บั๊ก เป็น debuggability tradeoff.

---

## สิ่งที่ตรวจแล้ว PASS (เพื่อ audit trail)

**Correctness — ledger keying (task_ledger + orchestrator):**
- `create_assignment`/`mark_done`/close ใช้ **full role name รวม shard suffix** (`qa#1`) สม่ำเสมอทั้ง 3 จุด → ไม่มี double-count/orphan ระหว่าง shard. `open` map key ด้วย role → แต่ละ shard คีย์แยก ✓
- Orphan/superseded fix (A7-followup): `_resolve_open_row` pop stale pointer → flip `superseded` ก่อนเปิด row ใหม่ → role มี open row ได้อย่างมาก 1 แถวเสมอ ✓
- `row_index` ยังใช้ได้เสมอเพราะ `feat["rows"]` append-only + group insert(0) ไม่ขยับ index ภายใน feature ✓
- `_derive_summary` รับ `raw_task_for_ledger` (ก่อน goal-prepend/codex-rewrite) → summary สะอาด, skip `[role:` + `รายงานกลับด้วย` ✓
- `_atomic_write` = temp(pid-suffixed) + `os.replace` (atomic same-fs, ปลอดภัยทั้ง Win/mac) ✓

**Security:**
- ไม่มี `shell=True` / `os.system` ทั้ง repo (grep ยืนยัน). subprocess ทุกจุดเป็น argv-list + timeout ✓
- `custom_roles.validate_role_name` reuse `config.validate_name` charset `[a-z0-9][a-z0-9_-]{0,63}` + reject `#` → custom role name หนี `CUSTOM_AGENTS_DIR`/`runtime/agents/<role>/` ด้วย traversal ไม่ได้ ✓
- `create_role` write order = temp-file → registry commit → rename-into-place; rename fail → roll back registry (กัน partial-commit ที่ทำให้ retry ชนว่า "already exists") ✓
- prior spawn_engine session-uuid traversal (F1) + env-builder audits = ยัง clean (ตรวจซ้ำจาก learned notes, ไม่มี regression) ✓

**Cross-platform (Win+macOS+Linux):**
- ทุก `sys.platform` gate ที่ตรวจมี branch ครบทั้ง 3 OS — CHROME_BIN probe (spawn_engine:1446 win/darwin/**linux else**), worktree symlink/junction (worktree_manager:326 win junction / else symlink), fonts fallback candidates (cockpit_theme) ครอบ Win/mac/Linux ✓
- `_display_path` ใน ledger normalize `os.sep` → `/` เพื่อ markdown link portable ✓
- ไม่มี hardcode `\\` หรือ `.exe` เปลือยในโค้ดใหม่ — ใช้ `pathlib.Path` ตลอด ✓

**Multi-provider (#103):**
- ledger `create_assignment` บันทึก `effective_provider` (หลัง degrade) → row สะท้อน provider จริงที่รัน ไม่ใช่ที่ขอ ✓
- `custom_roles` provider-neutral by design (spawn บน default claude) — documented tradeoff, ไม่ใช่ claude-only shortcut ที่ซ่อน ✓
- settings Providers&Roles view render substitution banner + degrade note ครบ ✓

**Consistency:**
- `task_dock.feature_emoji` ↔ `task_ledger._feature_emoji` ให้ผลตรงกันทุก status combo (working→🔨, fail→⚠️, all-terminal→✅) — ตรวจ set-comparison แล้วไม่ drift ✓
- `task_dock.project_progress` นับ `ok` อย่างเดียว = ตรงกับ `progress: X/Y` ที่ INDEX.md render ✓
- settings `_on_save_apply_clicked` transactional: snapshot 3 store → write → rollback ทุก store ถ้า fail กลางคัน; `set_role_items` คืน False → raise เพื่อ trigger rollback (ไม่ silent-continue); `regen_role_variants` รันเฉพาะตอน write สำเร็จครบ ✓

**lead_draft_state (byte-level state machine):**
- มือ 108/111/114 ครอบ: mouse SGR/X10 no-op, incomplete escape/UTF-8 tail buffering ข้าม PTY read, Ctrl+W/Alt-Backspace word_len tracking, bare-Esc over-clear (fail-safe direction ถูก: overcount→delay-only, undercount→clobber) — logic แม่นและ fail-safe bias ถูกทาง ✓
- draft-hold spill (lead_inbox:961) แยก not-ready จาก draft-blocked, spill durable + red-dot เมื่อ hold เกิน timeout ไม่ clobber draft ✓

---

## ปิดท้าย

โค้ดเบสนี้อยู่ในสภาพดีมากผิดปกติ — โค้ดรอบล่าสุดทุกไฟล์มี docstring อธิบาย "ทำไม" (ไม่ใช่แค่ "อะไร"), fail-safe direction ชัด, และมี test coverage ตาม (เห็นจาก tests/ ที่ commit คู่กันทุกรอบ). ไม่มีอะไรที่ควร block ship. ข้อเดียวที่ควรใส่ใจเชิงระบบคือ **A1 (orchestrator god-file โตกลับ)** — ไม่ใช่บั๊กแต่เป็น drift ที่ควรมี guardrail เชิงขนาดกันไว้ก่อนจะพอกกลับ 5.8k อีกรอบ.
