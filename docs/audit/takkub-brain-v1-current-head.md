# Takkub Brain V2 — Phase 0 Audit ของ HEAD ปัจจุบัน

> **AUDIT ONLY — ไม่มีการแก้โค้ด production ในรอบงานนี้สักบรรทัด.** ทุกข้อมีการอ้าง `file:line` จริงที่อ่านจากซอร์สโดยตรง ณ commit ด้านล่าง; ถ้าหาไม่เจอจะระบุว่า "หาไม่เจอ" ตรงๆ ไม่มีการเดา.

**Baseline:**
```
repo: takkub/agent-takkub
branch: main
commit: 0aee262a2b2648b248822e3bb587a49001b14166
version: 1.0.68  (pyproject.toml:3)
```
ตรงกับ baseline ที่ `docs/plans/takkub-brain-v2/00-CURRENT-BASELINE.md:8-9` อ้างไว้พอดี — HEAD ไม่ขยับ, symbol ทุกตัวที่แผนอ้าง (`digest_facts.py:29`, `orchestrator.subagent_done():1397`, `RUNTIME_DIR` `config.py:318`) มีอยู่จริงตามที่ตรวจยืนยันด้านล่าง.

---

## A. Hook point audit

### A.1 — common boundary ของ assign ที่ pane/subagent ใช้ร่วมกัน (Rule 1 ที่ต้องหาในเฟสนี้)

**`Orchestrator.assign()` — `src/agent_takkub/orchestrator.py:1472`** คือจุดเดียวที่ CLI `takkub assign` (ทั้ง `--mode pane` และ `--mode subagent`) เดินผ่านร่วมกัน — ก่อนแยกสาย:

```python
# orchestrator.py:1489-1508
if mode not in {"pane", "subagent"}:
    return False, "mode must be pane or subagent"
if mode == "subagent":
    ...
    return self._register_subagent(role_name, cwd, task, ...)
```

โค้ดตั้งแต่บรรทัด 1472 ถึงเช็ค `mode` ที่ 1489 (validate role/provider/model, resource-governor slot request, fan-out queue check ที่ 1534-1620) เป็นโค้ดที่ **ทั้งสอง mode วิ่งผ่านเหมือนกันทุกบรรทัด** ก่อนแยกไปที่:
- `mode="subagent"` → `self._register_subagent(...)` ที่บรรทัด 1498 (นิยามที่ `orchestrator.py:1261`)
- `mode="pane"` → `self._assign_with_worktree(...)` (isolation=worktree) หรือ `self._assign_dispatch(...)` ที่บรรทัด 1626/1640 (นิยาม `_assign_dispatch` ที่ `orchestrator.py:1705`)

**ข้อสังเกตสำคัญ:** resource-governor slot request (1536-1597) และ fan-out queue (1602-1620) ทำงาน**ก่อน**แยก mode — แปลว่าถ้า Brain ต้องการ build `BrainContextPack` ที่จุดเดียวจริงๆ ที่ครอบคลุมทั้งสอง mode โดยไม่ผูกกับ pane lifecycle ตาม Rule 1, จุดที่ถูกต้องคือ **ภายใน `assign()` ก่อนเช็ค `if mode == "subagent":` ที่บรรทัด 1491** (หรือทันทีหลังผ่าน resource-governor/fan-out gate แต่ก่อนแยกสาย) — ไม่ใช่ใน `_assign_dispatch` หรือ `_register_subagent` แยกกัน (ถ้าทำแยกกันจะต้อง duplicate logic 2 จุด).

### A.2 — assign pane path (`_assign_dispatch`, `orchestrator.py:1705-1963`)

Task-string transformation chain สำหรับ pane mode เรียงลำดับจริงตามที่อ่านโค้ด:

1. `raw_task_for_ledger = task` (1733) — เก็บต้นฉบับไว้ให้ ledger ก่อนแปลง
2. `task = self._apply_session_goal(task, project_ns)` (1734 → นิยาม `orchestrator.py:1248`) — ผูก session goal
3. `ps_assign.task_id = _uuid.uuid4().hex` (1745) — task_id (ephemeral UUID, ดู A.3)
4. resolve `effective_provider` (1775)
5. ถ้า `effective_provider == CODEX`: `task = _rewrite_task_for_codex(task)` (1779 → `orchestrator_text.py:446`)
6. `task = _append_verify_fail_hint(task, base_role_a)` (1780 → `orchestrator_text.py:478`)
7. ถ้า plan mode: `delivery_task = self._wrap_planner_task(task, plan_file, shard_total)` (1786 → `pipeline_executor.py:628`); ถ้า shard: `delivery_task = self._wrap_shard_task(task, shard_idx, shard_total)` (1792 → `pipeline_executor.py:656`)
8. `paste_text, task_file = _task_handoff_pointer(delivery_task, project_ns, role_name, supports_file_read=...)` (1802-1807 → นิยาม `orchestrator_text.py:517`) — **นี่คือจุดสุดท้าย**ที่แปลง `delivery_task` เป็น payload จริงที่จะ paste/preload เข้า pane
9. `spawn()` เรียกที่ 1838 (นิยามใน `spawn_engine.py:1393`)
10. **ภายใน `spawn()`** (แยกไฟล์ `spawn_engine.py`) — role-memory + MEMORY.md appendix ถูกประกอบเข้า **system-prompt file แยกต่างหาก** ไม่ใช่ `delivery_task`/`paste_text` (ดู A.10)

**ข้อสรุปสำคัญสำหรับ Rule 2:** "compose bounded Brain memory into the assignment before the existing delivery transformation" ตามแผนหมายความว่า Brain ต้องแทรกเข้า `task`/`delivery_task` **ระหว่างขั้นตอน 6 กับ 7** (หลัง verify-fail-hint, ก่อน `_task_handoff_pointer`) — ถ้าแทรกหลังขั้นตอน 8 จะไม่ทันถูกเขียนลง handoff file/paste_text; ถ้าแทรกก่อนขั้นตอน 5 จะโดน `_rewrite_task_for_codex` ตัดต่อทับซ้ำโดยไม่ตั้งใจ (ฟังก์ชันนี้ไม่รู้จัก Brain block).

### A.3 — task_id / ledger identity (สำคัญ: ไม่ใช่สิ่งเดียวกัน)

มี "task_id" 2 ความหมายที่ไม่เกี่ยวกันเลย ต้องแยกให้ชัดก่อนออกแบบ `MemoryEvent.task_id` / `ContinuationRecord.task_id`:

- **PaneState.task_id (pane mode):** `ps_assign.task_id = _uuid.uuid4().hex` ที่ `orchestrator.py:1745` — UUID สุ่มใหม่ทุกครั้งที่ assign, เก็บใน `PaneState`, **ไม่เคยถูกส่งเข้า `task_ledger.create_assignment()` เลย** (ดูลายเซ็นฟังก์ชันด้านล่าง — ไม่มีพารามิเตอร์ task_id)
- **subagent task_id:** `task_id = _uuid.uuid4().hex` ที่ `orchestrator.py:1324` — เก็บใน `self._subagent_assignments[(project_ns, role_name)]["task_id"]`, ใช้แค่ตั้งชื่อไฟล์ capsule (`capsule_path`, บรรทัด 1334-1336) — ก็ไม่ถูกส่งเข้า ledger เช่นกัน
- **task_ledger identity:** `create_assignment(project, role, cwd, task, goal, feature, provider)` — นิยามที่ `task_ledger.py:182-190` **ไม่มีพารามิเตอร์ task_id เลย**. ตัวระบุ assignment ที่แท้จริงคือ `(project, role)` — ดู `state.setdefault("open", {})[role] = {...}` ที่ `task_ledger.py:254` และการ resolve stale open row ด้วย `_resolve_open_row(state, project, role, ...)` ที่ `task_ledger.py:247` (คีย์ด้วย role, ไม่ใช่ task_id)

**ผลกระทบต่อแผน:** `ContinuationRecord.task_id: str | None` (02-DATA-MODEL.md:72) และ `MemoryEvent.task_id` (02-DATA-MODEL.md:17) จะต้องตัดสินใจว่าจะอ้างอิง PaneState.task_id (ephemeral, หายไปตอน done() pop ที่ `orchestrator.py:3386`) หรือ subagent task_id (เก็บใน dict แยก) — **ไม่มี "task_id" ที่เป็นแหล่งความจริงเดียว (single source of truth) ในระบบปัจจุบัน** ทั้งสองค่าเป็นแค่ label ชั่วคราวของ instance การ assign หนึ่งครั้ง ไม่ใช่ identity ของ "งาน" ในความหมายที่ ledger เข้าใจ (ledger เข้าใจแค่ role). ถ้า Brain ต้องการ exact-continuation ที่แม่นตาม "งานเดียวกัน" จริง ต้อง mint identity ใหม่เอง ไม่สามารถยืมจาก 2 ตัวที่มีอยู่ได้ตรงๆ.

### A.4 — assign subagent path (`_register_subagent`, `orchestrator.py:1261-1395`)

- `task = self._apply_session_goal(task, project_ns)` (1323) — ได้ transformation เดียวกับ pane mode ข้อ (2)
- **ไม่มี** `_rewrite_task_for_codex`, **ไม่มี** `_append_verify_fail_hint`, **ไม่มี** shard/plan wrap, **ไม่มี** `_task_handoff_pointer` — capsule text ถูกประกอบตรงๆ ด้วย f-string ที่ `orchestrator.py:1325-1332` (role decl + cwd + task + คำสั่ง `takkub subagent-done`)
- เขียนไฟล์ capsule ที่ `capsule_path` (`orchestrator.py:1333-1340`) ใต้ `_task_handoff_dir(project_ns)` (นิยาม `orchestrator_text.py:500`)
- **ไม่เคยเรียก `spawn()`** เพราะ subagent ไม่มี pane — แปลว่า role-memory appendix (A.10) และ MEMORY.md pointer **ไม่ถูกฉีดเข้า subagent เลยในปัจจุบัน** (ยืนยันจากการอ่านทั้งฟังก์ชัน 1261-1395 ไม่มี import `role_memory` เลยสักบรรทัด)

**ช่องว่างที่พิสูจน์แล้ว (ไม่ใช่เดา):** วันนี้ subagent ได้ context น้อยกว่า pane อย่างเป็นระบบ — ไม่มี role-memory, ไม่มี MEMORY.md pointer, ไม่มี verify-fail-hint. นี่คือ**เหตุผลเชิงโครงสร้างจริง** (ไม่ใช่แค่ตามแผนพูดลอยๆ) ว่าทำไม Brain ต้อง hook ที่ `assign()` boundary (A.1) ไม่ใช่ที่ `spawn()`.

### A.5 — done path (`Orchestrator.done()`, `orchestrator.py:3306-3511+`)

Flow ที่ยืนยันจากอ่านโค้ดจริง (บรรทัดอ้างอิงในวงเล็บ):
1. validate role, ปฏิเสธถ้าเป็น LEAD (3310-3314)
2. capture `origin_pane_token` **ก่อน** teardown (3331, ใช้กัน #228 race)
3. release resource-governor slot (3335-3337), cancel delivery-manager tracking (3339-3345)
4. อ่าน `PaneState` fields **ก่อน pop** (3348-3363) — รวม `had_task_id = _ps_done.task_id or ... or f"pane-{id(pane)}"` (3362) — สังเกตว่ามี fallback เป็น `id(pane)` ถ้า task_id หาย ยิ่งตอกย้ำว่า task_id ไม่ใช่ identity ที่เสถียร
5. `extract_issue_ref(_ps_done.last_assigned_task)` (3371) — ref มาจาก assign spec เดิม ไม่ใช่จาก note ของ agent
6. pop `_pane_state`/`_idle_state` (3385-3386)
7. screenshot evidence scan → fold เข้า `note` (3394-3396)
8. `task_ledger.mark_done(project_ns, from_role, "fail"/"ok", ts=now)` (3407-3414)
9. `session_md_path = self._save_decision_note(...)` (3416-3418) — **นี่คือจุดเขียนไฟล์ note ที่ subagent_done() ก็เรียกร่วมกัน** (ดู A.9)
10. ถ้า `failed`: **ไม่คำนวณ DigestFacts** (`digest_facts = None` ตั้งไว้ล่วงหน้าที่ 3440, ไม่ถูก assign ใหม่ใน branch failed) + เรียก `role_memory.append_failure_entry(project_ns, fail_role, fail_reason)` (3472-3476) — **เฉพาะ pane mode** เท่านั้น (subagent_done ไม่มีบรรทัดนี้ — ดู A.9)
11. ถ้าไม่ failed: คำนวณ `digest_facts` ผ่าน `self._compute_digest_facts(...)` (3487, นิยามที่ 3161) — มี try/except คลุม, ถ้า error จะ fallback เป็น `DigestFacts` เปล่าที่มีแค่ `files_note` บอกว่าคำนวณไม่ได้ (3502-3510)

### A.6 — subagent_done path (`Orchestrator.subagent_done()`, `orchestrator.py:1397-1470`)

Flow: validate role → pop `pending[(project_ns, role_name)]` (1407) → ถ้าไม่พบ คืน error → `task_ledger.mark_done(...)` (1413-1418, **เหมือน done() ทุกประการ ทั้งสองเรียกฟังก์ชันเดียวกัน**) → `session_path = self._save_decision_note(...)` (1424, **เรียกฟังก์ชันเดียวกับ done() บรรทัด 3416**) → build notice → shard-group bookkeeping (1445-1458) → `self._write_hot_md()` (1467) → `self.agentDone.emit(...)` (1468)

**ไม่มี** `_compute_digest_facts` เลยในฟังก์ชันนี้ (ยืนยันจากอ่านทั้ง 1397-1470 ไม่มี `digest_facts` หรือ import ของ `digest_facts` module สักที่) — ตรงกับที่ Rule 4 ในแผนเขียนว่า "For **pane-mode clean done**" เท่านั้น แต่ยังไม่มีกลไก outcome-provenance อะไรเทียบเท่าสำหรับ subagent เลย (ไม่ใช่แค่ "ยังไม่ reuse DigestFacts" — subagent ไม่มี git-fact ใดๆ ถูกวัดเลยตอน done).

**ไม่มี** `role_memory.append_failure_entry` เรียกใน subagent_done ด้วย (แม้จะมี `failed` parameter ที่ 1398) — เทียบกับ pane's done() ที่มี (A.5 ข้อ 10). **นี่คือ asymmetry จริงอีกจุด**: pane failure สอน role_memory, subagent failure ไม่สอนอะไรเลย.

### A.7 — จุดสร้าง DigestFacts

- **นิยาม dataclass:** `DigestFacts` — `src/agent_takkub/digest_facts.py:29` (frozen dataclass, provenance คือ "everything one digest bullet needs, pre-computed by `done()`")
- **จุดสร้าง instance จริง:** `Orchestrator._compute_digest_facts()` (static method, `orchestrator.py:3161-3304`) — สร้าง `DigestFacts(...)` 4 จุดต่างกันตาม branch (worktree pane: 3215; missing-snapshot fallback: 3248; porcelain-read-fail fallback: 3267; shared-tree ปกติ: 3287)
- **เรียกจาก:** `done()` เท่านั้น ที่ `orchestrator.py:3487` ภายใต้เงื่อนไข **ไม่ failed** (ดู A.5 ข้อ 10-11) — มี error-guard fallback ที่ 3498-3510
- **ไม่เคยเรียกจาก** `subagent_done()` (A.6), digest-flush, Lead UI, inbox read, หรือ wait poll — สอดคล้องกับ Rule 3/4 ที่ห้าม capture จากจุดเหล่านั้น

### A.8 — จุดสร้าง task capsule

`orchestrator.py:1325-1336` ภายใน `_register_subagent()` — capsule text ประกอบด้วย f-string ตรงๆ (role decl, "You are a native subagent...", cwd, task, คำสั่ง `takkub subagent-done`, hint เรื่อง save findings ใต้ docs/) → เขียนไฟล์ที่ `capsule_path = capsule_dir / f"{HHMMSS}-{role_name}-subagent-{task_id[:8]}.md"` (1334-1336) ใต้ `_task_handoff_dir(project_ns)` (`orchestrator_text.py:500`, path = `RUNTIME_DIR/tasks/<project>/<date>/`) → เขียนจริงที่ `capsule_path.write_text(capsule, encoding="utf-8")` (1338)

### A.9 — ลำดับ provider task transformation (ครบทั้งสองสาย)

สรุปเป็นตาราง — pane mode มี 6 การแปลง, subagent มี 1:

| ลำดับ | Pane mode (`_assign_dispatch`) | Subagent mode (`_register_subagent`) |
|---|---|---|
| 1 | `_apply_session_goal` (1734) | `_apply_session_goal` (1323) |
| 2 | `_rewrite_task_for_codex` ถ้า CODEX (1779, `orchestrator_text.py:446`) | — ไม่มี |
| 3 | `_append_verify_fail_hint` (1780, `orchestrator_text.py:478`) | — ไม่มี |
| 4 | `_wrap_planner_task`/`_wrap_shard_task` ถ้ามี (1786/1792) | — ไม่มี |
| 5 | `_task_handoff_pointer` (1802, `orchestrator_text.py:517`) | — ไม่มี (capsule ไม่ผ่าน pointer mechanism เลย เพราะไม่มี pane ที่จะ "paste-swallow" ได้) |
| 6 | role-memory + MEMORY.md appendix (**spawn-time**, `spawn_engine.py`, ดู A.10) | — ไม่มี (ไม่เคย `spawn()`) |
| — | capsule wrapping (role/cwd/completion-command f-string) | ✅ `orchestrator.py:1326-1331` |

### A.10 — จุด inject role memory

`spawn_engine.py`, ภายในเมธอด `spawn()` (นิยามเริ่ม `spawn_engine.py:1393`), บล็อกจริงที่ `spawn_engine.py:2044-2169`:
- `_mem_path` (project MEMORY.md pointer, auto-memory ของ Claude Code) ถูกฉีดเป็น**ตัวชี้** (ไม่ใช่เนื้อหาเต็ม) ที่ 2044-2062
- `ensure_role_memory(project_ns, base_role)` (2068-2070, นิยาม `role_memory.py:604`) อ่าน/สร้างไฟล์ L1
- `has_learned_content(...)` (2095-2097, `role_memory.py`) เช็คว่ามีเนื้อหาจริงหรือแค่ skeleton — ถ้าว่างเปล่าจะฉีดแค่ 1 บรรทัด pointer (2107-2118) แทนเนื้อหาเต็ม (ประหยัด ~100-150 tok/spawn ตามคอมเมนต์ที่ 2088-2093)
- ถ้ามีเนื้อหา: inline เต็ม (ตัดที่ 200 บรรทัดท้ายสุด, `_MEM_MAX_LINES = 200` ที่ 2120) เข้า `_appendix` string (2119-2148) — string นี้ต่อท้าย **system-prompt file** ของ spawn (ไม่ใช่ `delivery_task`/`paste_text` จาก A.2)
- `consume_distill_pending(...)` (2156-2158) เช็ค flag แจ้งเตือนให้กลั่นความรู้เก่า

**ยืนยันซ้ำ:** นี่คือ**spawn-time**, ผูกกับ `spawn()` ไม่ใช่ `assign()` — pane ที่ยัง alive อยู่แล้วรับ assignment ใหม่ (ไม่ผ่าน `spawn()` เพราะ `pane_is_running=True` แล้ว, ดู `orchestrator.py:1746-1751`) **จะไม่ได้ role-memory refresh ใหม่เลย** เพราะ inject เกิดครั้งเดียวตอน spawn ครั้งแรกเท่านั้น. นี่คือหลักฐานจริงที่ยืนยัน 00-CURRENT-BASELINE.md ข้อ 2 ("spawn-time Brain only = ผิด") ตรงตัวอักษร — role_memory มี "บั๊ก" แบบเดียวกันนี้อยู่แล้วในปัจจุบัน.

### A.11 — import-linter contracts ที่จะโดนผลกระทบ

**นับจริงจาก `pyproject.toml`: 25 contracts** (ไม่ใช่ 23 ตามที่ root `CLAUDE.md` เขียนไว้ — เลขใน CLAUDE.md เก่ากว่าจำนวนจริงตอนนี้ อาจไม่ได้อัปเดตหลัง contract ล่าสุดถูกเพิ่ม; ตรวจนับด้วย `grep -c '\[\[tool.importlinter.contracts\]\]' pyproject.toml` = 25).

Contracts ที่เกี่ยวข้องโดยตรงกับ package ใหม่ `agent_takkub.brain/` (`pyproject.toml:128-547`):

| Contract | source_modules | forbidden_modules (ที่เกี่ยว) | ผลต่อ Brain |
|---|---|---|---|
| `cli-ipc-boundary` (141) | `agent_takkub.cli` | `agent_takkub.orchestrator` | Phase 2 CLI (`brain remember/search/show/trace/redact`) **ห้าม** ให้ `cli.py` import `agent_takkub.brain.*` ถ้า `brain/*` เองดันไป import `orchestrator` (จะกลายเป็น indirect violation) — ต้องให้ `brain/` เป็น leaf ที่ไม่แตะ orchestrator เลย |
| `leaf-modules-pure` (149-210) | `config`, `_win_console`, `roles`, `system_baseline` | รายชื่อยาวรวม `role_memory` (193) | `brain/paths.py` จะ import `config.RUNTIME_DIR` แน่นอน (leaf→leaf ผ่านได้ปกติ ทิศทางถูก) แต่ **`brain/` ต้องไม่ถูกเพิ่มเข้า `source_modules` ของ contract นี้** เพราะ `config.py` เองต้องไม่ import กลับเข้า `brain` (จะกลับทิศ) |
| `worktree-manager-leaf` (286-302) | `worktree_manager` | `orchestrator`, `spawn_engine`, ... | ตัวอย่าง contract รูปแบบเดียวกับที่ `brain/` น่าจะต้องมี — leaf ที่ถูก import จากทั้ง orchestrator (engine) และอาจจาก cli (ต้อง contract คู่กันกันไม่ให้ `brain` import ย้อนกลับเข้า `orchestrator`/`spawn_engine`/`main_window`/`app`/`cli` |
| `provider-spec-pure` (353-372) | `provider_spec` | รายชื่อยาว รวม `orchestrator`, `spawn_engine`, `pty_session`, `orchestrator_text`, `lead_inbox` | ตัวอย่าง pure-data-layer contract — ถ้า Brain models.py เก็บ enum/dataclass ล้วน (`MemoryEvent`, `ContinuationRecord`) ควรพิจารณาทำ contract แบบเดียวกันสำหรับ `agent_takkub.brain.models` โดยเฉพาะ ถ้าจะให้หลายเลเยอร์ import type ได้อย่างปลอดภัย |
| `spawn-engine-layer` (374-385) | `spawn_engine` | `orchestrator`, `main_window`, `app`, `cli` | ถ้า Phase 4 (assignment integration) ต้องแตะ `spawn_engine.py` ด้วย (เช่น role-memory injection point เดิมที่ A.10 ต้องรวมกับ Brain hot-context) — `spawn_engine` เองก็ห้าม import orchestrator เช่นกัน ต้อง import brain ผ่านทางเดียวกับที่ orchestrator ทำ ไม่ผ่านกันเอง |

**สรุปสำหรับ Phase 1:** package ใหม่ `agent_takkub.brain/` ควรถูกเพิ่ม `[[tool.importlinter.contracts]]` ใหม่เองอย่างน้อย 1 ตัว (รูปแบบเดียวกับ `worktree-manager-leaf`/`provider-spec-pure`) ที่ห้าม `agent_takkub.brain` import `orchestrator`, `spawn_engine`, `main_window`, `app`, `cli` — เพราะ Brain จะถูก import จากทั้ง orchestrator (engine, Rule 1/3) **และ** cli (Phase 2 CLI commands) **และ** อาจจาก spawn_engine (ถ้า hot-context ต้องรวมกับ role-memory injection) พร้อมกัน — สามทิศทางเข้าเหมือน `worktree_manager`/`provider_spec` เป๊ะ. import-linter's `forbidden` contract type เช็คแบบ transitive (indirect imports นับด้วย เว้นแต่จะตั้ง `ignore_imports` ยกเว้นเฉพาะจุด — เจอการใช้ `ignore_imports` แค่ 1 จุดทั้งไฟล์ที่ `pyproject.toml:473`, ไม่เกี่ยวกับ brain) ดังนั้นแค่ import อ้อมผ่าน `role_memory`/`task_ledger` ก็ต้องเช็คว่าไม่ไปโดน edge ต้องห้ามด้วย.

---

## B. ตาราง "อะไรเก็บที่ไหน" (write routing)

| ที่เก็บ | เขียนโดยใคร (function, file:line) | Trigger point | คีย์/identity | pane หรือ subagent |
|---|---|---|---|---|
| **role_memory L1** (`RUNTIME_DIR/role-memory/<project>/<role>.md`) | (a) ตัว agent เองผ่าน `Edit`/`Write` tool ตามคำสั่งใน appendix — ไม่ใช่ cockpit code · (b) `role_memory.append_failure_entry()` เรียกจาก `orchestrator.py:3472` | (a) ระหว่างทำงาน ตาม agent ตัดสินใจเอง · (b) `done(failed=True)` **เฉพาะ pane** | `(project_ns, base_role)` — project_ns = short name จาก `projects.json` | **pane only** — subagent ไม่มีทั้งอ่าน(A.4)และเขียน(A.6) |
| **role_memory L2 archive** | `_archive_entries()` เรียกจาก `ensure_role_memory()` ที่ `role_memory.py:626` | **อ่าน**-time (spawn) เป็นผล side-effect ของการ curate L1 ทุกครั้งที่ spawn | เดียวกับ L1 | pane only (ผูกกับ spawn) |
| **task_ledger** (`RUNTIME_DIR/tasks/<project>/`) | `task_ledger.create_assignment()` (`task_ledger.py:182`) เรียกจาก **ทั้งสองสาย**: `orchestrator.py:1887` (pane) และ `orchestrator.py:1360` (subagent) · ปิดด้วย `task_ledger.mark_done()` (`task_ledger.py:331`) เรียกจาก `orchestrator.py:3409` (pane done) และ `orchestrator.py:1415` (subagent done) | assign-time (open) + done-time (close) | `(project, role)` — **ไม่มี task_id**, resolve stale-open ด้วย role เท่านั้น (`_resolve_open_row`) | **ทั้งสองสาย เท่ากัน** — ระบบเดียวที่มี write-path สมมาตรจริง วันนี้ |
| **vault_mirror** (Obsidian, `$TAKKUB_VAULT_DIR` หรือ `~/second-brain`) + local `.md` (`RUNTIME_DIR/sessions/<date>/<project>/<role>-<HHMMSS>.md`) | `_save_decision_note()` (`orchestrator.py:3854`) เรียกจาก **ทั้งสองสาย**: `orchestrator.py:3416` (done) และ `orchestrator.py:1424` (subagent_done) | completion-time, best-effort (junk-note/junk-project filter ที่ 3892-3897 อาจ skip) | `(project, role, timestamp)` filename | **ทั้งสองสาย** — เป็น**จุดร่วมที่มีอยู่แล้วจริง**สำหรับ Rule 3's "shared internal completion façade" |
| **MEMORY.md auto-memory** (Claude Code's own, `~/.claude-work/projects/<encoded-cwd>/memory/`) | Claude teammate LLM เอง ผ่าน Write tool ตาม system-prompt instructions (ไม่ใช่ cockpit code) | เมื่อ Claude เห็นข้อมูลควรจำ (LLM ตัดสินใจเอง) | Claude Code's own `encode_path_for_claude`-based dir | **Claude provider เท่านั้น** — provider อื่น (codex/gemini/opencode/kimi/cursor) ไม่มีระบบนี้เลย (gap ที่รู้อยู่แล้ว, ไม่ใช่ของใหม่จาก audit นี้) |
| **chatlog_scanner** | **ไม่เขียนอะไรเลย** — grep ยืนยันแล้ว: `write_text`/`.write(`/`json.dump` ใน `chatlog_scanner.py` = 0 ที่ | อ่านอย่างเดียว (`iter_session_files`, `decode_project_dir`) | อ่านจาก Claude's session jsonl ใต้ `claude_projects_dir()` | **read-only source สำหรับ COLD retrieval** ไม่ใช่ store |
| **Brain (ใหม่, ตามแผน)** | `brain/capture.py` façade — ตามแผนควรแนบกับจุดเดียวกับ vault_mirror (`_save_decision_note` boundary) หรือ done()/subagent_done() ตรงๆ | completion-time (Rule 3) + assign-time สำหรับ ContinuationRecord read | `encode_path_for_claude(cwd)` — **ไม่ใช่ project_ns** (ดู Section D) | ต้องออกแบบให้ครอบทั้งสองสาย ตาม A.4/A.6 ที่พิสูจน์แล้วว่าไม่สมมาตรวันนี้ |

### จุดที่จะซ้ำซ้อนกัน + วิธีตัด

1. **role_memory L1 (lessons/gotchas/decisions) ↔ Brain MemoryEvent kind `lesson`/`pattern`/`decision`/`architecture`** — overlap สูงสุด ทั้งคู่คือ "ความรู้สะสมต่อ (project, role)". 04-RETRIEVAL-AND-TOKEN-BUDGET.md:93-94 บอกกฎอ่าน ("ถ้าซ้ำ ให้ Brain event ชนะ") แต่**ไม่ได้บอกกฎเขียน** — ถ้าทั้งสองระบบยัง auto-write คู่ขนานกัน (agent เขียน role_memory เองตามเดิม + Brain capture เขียนอีกชุด) ความรู้จะซ้ำสองที่ถาวร ไม่ใช่แค่ตอนอ่าน. **ข้อเสนอ:** Phase 5 ต้องตัดสินใจอย่างใดอย่างหนึ่งชัดเจน — (ก) role_memory หยุดรับ auto-write จาก agent ต่อ แล้วให้ Brain capture เป็นเจ้าของ single-write-path, แสดงผล role_memory L1 เป็น *view ที่ render จาก Brain events* แทน หรือ (ข) Brain capture ไม่แตะ role_memory เลย ปล่อยให้ agent เขียนต่อไปตามเดิม แต่ Brain event ต้อง**อ้างอิง**ว่ามาจาก role_memory (provenance `agent_reported`) ไม่ generate ซ้ำเอง. แผนปัจจุบันยังไม่ระบุว่าเลือกทางไหน — เป็นช่องว่างจริงที่ต้องปิดก่อน Phase 5
2. **role_memory failure-entry (`append_failure_entry`, pane only, A.5#10) ↔ Brain outcome capture (Rule 3, ต้องครอบทั้งสองสาย)** — ถ้า Brain แก้ asymmetry นี้ (เพิ่ม subagent failure capture) แต่ role_memory เดิมไม่แก้ตาม จะเกิดสถานการณ์ที่ Brain รู้เรื่อง subagent failure แต่ role_memory (ที่ inject เข้า spawn prompt) ไม่รู้ — **ไม่ใช่ปัญหาทันที** เพราะ subagent ไม่เคยเห็น role_memory อยู่แล้ว (A.4) แต่ pane ตัวถัดไปของ role เดียวกันจะเห็น role_memory จาก pane-failure เท่านั้น ไม่เห็น subagent-failure ที่ Brain บันทึกไว้ — ต้องตัดสินใจว่า Brain hot-context (Phase 3/4) จะ merge ทั้งสองแหล่งตอนอ่านหรือไม่
3. **vault_mirror ↔ Brain** — ทั้งคู่ผูกกับ `_save_decision_note` boundary เดียวกัน (completion-time) แต่ consumer ต่างกัน (มนุษย์เปิด Obsidian vs agent query ผ่าน `takkub brain search`) — **ไม่ต้องตัด** เพราะเป็น sink คนละประเภท (human-readable mirror vs machine-queryable index) ทั้งคู่อ่าน `note` เดียวกันเป็น input แต่ output ไม่ทับกัน
4. **task_ledger ↔ Brain** — task_ledger เป็น "สถานะงานตอนนี้" (`[~]`/`[✓]`/`[✗]` ต่อ role, ไม่มี semantic content) ส่วน Brain เป็น "ทำไมถึงเป็นแบบนี้/รู้อะไรบ้าง" — ไม่ทับกันโดยธรรมชาติ, Brain ควรอ้าง task_ledger row (ผ่าน `(project, role, timestamp)`) เป็น provenance ไม่ generate ledger-like state ซ้ำ

---

## C. Token budget วัดจริง (cl100k, tiktoken)

**วิธีวัด:** `tiktoken.get_encoding("cl100k_base")` — วิธีเดียวกับที่ใช้ใน `docs/audit/2026-08-16-token-cost-measurement.md:160` (การวัด #267 CLAUDE.md diet) เพื่อให้เทียบตัวเลขกันได้ตรงๆ. รันจาก system Python (`tiktoken` ติดตั้งอยู่แล้วนอก venv โปรเจกต์ — **ไม่ใช่ dependency ของ `pyproject.toml`** ตรวจแล้วด้วย grep, ไม่มีคำว่า `tiktoken` ในไฟล์นั้นเลย — ถ้า production code ของ Brain ต้องการวัด token เอง (เช่น enforce budget runtime) จะต้องเพิ่มเป็น dependency จริงในเฟสถัดไป, การวัดในเอกสารนี้เป็น audit-time เท่านั้นไม่ได้แตะ production).

### C.1 — ยืนยัน chars/token ratio ของอังกฤษ/ไทย ด้วยข้อมูลจริง (ไม่ใช่ synthetic ล้วน)

| ตัวอย่าง | chars | tokens | chars/token |
|---|---:|---:|---:|
| EN synthetic prose (ประโยคเทคนิคทั่วไป, x4) | 1,372 | 233 | **5.888** |
| TH synthetic prose (ประโยคธรรมชาติ ไม่มีคำอังกฤษปน, x4) | 1,168 | 1,040 | **1.123** |
| `digest_facts.py` เต็มไฟล์ (โค้ด+docstring อังกฤษ) | 5,890 | 1,517 | 3.883 |
| **`runtime/role-memory/agent-takkub/backend.md`** (เนื้อหาจริงที่ inject เข้า spawn prompt วันนี้ — Thai prose + English code identifiers ปนกัน) | 5,610 | 1,570 | **3.573** |
| root `CLAUDE.md` (agent-takkub, Thai+English ปนกัน) | 2,109 | 966 | 2.183 |

**ข้อสรุป — ตัวเลขในตัว issue (5.47 EN / 2.00 TH) อยู่ในช่วงที่สมเหตุสมผลแต่เป็นกรณีสุดโต่งสองขั้ว ไม่ใช่กรณีที่ Brain content จะเจอจริง:**
- ค่า EN-pure ที่วัดได้ (5.89) ใกล้เคียงกับ 5.47 ที่ issue อ้าง — ตรงกัน
- ค่า TH-pure ที่วัดได้ (1.12) **แย่กว่า** 2.00 ที่ issue อ้างด้วยซ้ำ (ประโยคไทยธรรมชาติไม่มีเว้นวรรคระหว่างคำ ทำให้ BPE token หนักกว่า)
- **ตัวเลขที่สำคัญที่สุดคือ 3.573** — วัดจาก role_memory ไฟล์จริงที่ระบบ inject เข้า spawn prompt อยู่ทุกวันนี้ (เนื้อหาแบบเดียวกับที่ Brain event/lesson จะมี: บรรทัดไทยปนกับ `code_identifier`, `file.py:123`, ชื่อฟังก์ชัน) — **นี่คือ ratio ที่ควรใช้ประมาณ budget จริง ไม่ใช่ 5.47 หรือ 2.00 เพียวๆ**

### C.2 — แปลง budget ที่แผนเสนอ (chars) เป็น token จริง ด้วย 3 สมมติฐาน ratio

Budget เดิมจาก `04-RETRIEVAL-AND-TOKEN-BUDGET.md:56-61`:
```
total Brain assignment block: 2,500–4,000 chars
exact continuation:           up to 1,800 chars
project constraints/decisions: 1,200 chars
role lessons:                  800 chars
```

| Budget (chars) | @ EN-pure 5.89 chars/tok | @ mixed-real 3.57 chars/tok (แนะนำ) | @ TH-pure 1.12 chars/tok |
|---|---:|---:|---:|
| total 2,500–4,000 | 424–679 tok | **700–1,120 tok** | 2,232–3,571 tok |
| exact continuation ≤1,800 | 306 tok | **504 tok** | 1,607 tok |
| project constraints ≤1,200 | 204 tok | **336 tok** | 1,071 tok |
| role lessons ≤800 | 136 tok | **224 tok** | 714 tok |

**ผลต่างสูงสุด 5.3 เท่า** ระหว่างสมมติฐาน EN-pure กับ TH-pure สำหรับ budget เดียวกัน — พิสูจน์ตรงตัวว่าหน่วย chars "ใช้กับเราไม่ได้" ตามที่ issue ตั้งข้อสงสัยไว้ ไม่ใช่แค่ความเห็น

### C.3 — เทียบกับ savings จาก #267 (5,780 tok/spawn)

ตัวเลข 5,780 tok/spawn ยืนยันจริงที่ `docs/audit/2026-08-16-token-cost-measurement.md:167`: *"non-Lead pane ประหยัด **6,728 − 948 = 5,780 tok/spawn**"* (วัดด้วย tiktoken cl100k_base เหมือนกัน, root `CLAUDE.md` diet).

| สมมติฐาน ratio | Brain total budget (4,000 chars) เป็น tok | % ของ 5,780 ที่ "คืนกลับ" |
|---|---:|---:|
| EN-pure (5.89) | 679 tok | **~12%** |
| **mixed-real (3.57, แนะนำ)** | **1,120 tok** | **~19%** |
| TH-pure (1.12) | 3,571 tok | **~62%** |
| issue's ตัวเลขอ้างอิง (2.00) | 2,000 tok | **~35%** |

**ข้อสรุป:** ตัวเลข "35%" ที่ issue อ้างอิงมาจาก ratio 2.00 ซึ่งเป็นค่ากลางๆ ที่ไม่ตรงกับทั้ง EN-pure ที่วัดได้ (5.89) หรือ mixed-content จริงที่วัดได้จาก role_memory (3.57). **ด้วยข้อมูลจริงที่มีตอนนี้ ตัวเลขที่น่าเชื่อถือที่สุดคือ ~19% (ไม่ใช่ 12% หรือ 35%)** — แต่ทั้งหมดนี้ยังเป็นการประมาณจาก budget เป้าหมาย (chars ที่ตั้งไว้ก่อนเขียนโค้ดจริง) ไม่ใช่เนื้อหา Brain จริงที่ยังไม่มีอยู่ — **ตัวเลขจะแม่นได้จริงก็ต่อเมื่อวัดจาก payload ที่ Brain compose จริงใน Phase 4** ตรงตามที่ issue เสนอว่า measurement gate ควรอยู่ Phase 4

### C.4 — ข้อเสนอ: budget หน่วย token แทน chars

แนะนำตั้ง budget เป็น **token ceiling ตรงๆ** (วัดด้วย cl100k จริงตอน compose ไม่ใช่ประมาณจาก chars):
```
total Brain assignment block: 700–1,120 tok   (ใช้ตัวเลข mixed-real เป็นฐาน)
exact continuation:           ≤500 tok
project constraints/decisions: ≤350 tok
role lessons:                  ≤250 tok
```
พร้อม **hard enforcement**: `context.py` ต้อง encode ด้วย cl100k จริงก่อนประกอบเข้า assignment แล้วตัด/priority-drop ตามลำดับที่ `04-RETRIEVAL-AND-TOKEN-BUDGET.md:69-76` ระบุไว้ (exact continuation > hard constraint > active decision > architecture > role lesson > historical note) — ไม่ใช่ตัด chars ตรงๆ เพราะ chars-ceiling ที่ตั้งจาก assumption ผิดภาษาจะปล่อยให้เนื้อหาไทยล้น budget จริงถึง 3 เท่าโดยไม่รู้ตัว (ดู C.2)

**Measurement gate:** เห็นด้วยกับ issue — ควรอยู่ **Phase 4** (ตอน `context.py` ประกอบ assignment จริงเป็นครั้งแรก) ไม่ใช่ Phase 8 เพราะ Phase 4 เป็นจุดแรกที่มี payload จริงให้วัด ก่อนหน้านั้น (Phase 1-3) เป็นแค่ storage/CLI ที่ไม่เคย inject เข้า assignment เลย วัดตอนนั้นวัดอะไรไม่ได้จริง

---

## D. Project path identity + Windows safety

### D.1 — key ต้องใช้ `encode_path_for_claude` เท่านั้น (ยืนยันตามที่ issue สั่ง)

**นิยาม:** `token_meter.py:95-110`
```python
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")  # token_meter.py:92
def encode_path_for_claude(cwd: str | Path) -> str:
    return _NON_ALNUM_RE.sub("-", str(Path(cwd).resolve()))
```
แทนที่ทุกตัวอักษรที่ไม่ใช่ `[A-Za-z0-9]` ด้วย `-` บน**ผลลัพธ์ของ `Path(cwd).resolve()` เต็ม path** (ไม่ใช่แค่ชื่อโปรเจกต์สั้นๆ) — ตัวอย่างจริงจาก docstring: `C:\Users\alice\WebstormProjects\my_app_web\client` → `C--Users-alice-WebstormProjects-my-app-web-client`

**เหตุผลที่ issue สั่งห้ามใช้ `decode_project_dir`:** `chatlog_scanner.py:50-66` — ฟังก์ชันนี้ reverse-map `-` กลับเป็น path separator แบบ**ไม่สามารถแยกแยะได้**ว่า `-` ตัวไหนคือ separator เดิม กับตัวไหนคือ literal `-`/`_`/`.`/space ที่ถูก encode มา (ยืนยันจาก docstring ของ `session_project_dir_for_cwd` ที่ `token_meter.py:120-122`: *"`chatlog_scanner.decode_project_dir()` is lossy"*) — โปรเจกต์ที่ชื่อมี `-` `_` `.` หรือเว้นวรรคจะ round-trip ผิดแน่นอน (ตรงกับ memory `decode-project-dir-lossy.md`)

### D.2 — Mismatch สำคัญที่ต้องรู้ก่อนออกแบบ: `project_ns` (ที่ระบบอื่นทั้งหมดใช้) ≠ `encode_path_for_claude(cwd)` (ที่ Brain ต้องใช้)

พิสูจน์จากโค้ดจริง — ทุกระบบใน Section B ใช้ `project_ns` (ชื่อสั้นจาก `projects.json`, validate ด้วย `validate_name()` ที่ `config.py:50`) เป็นคีย์ path โดยตรง:
- `task_ledger._ledger_dir(project)` → `RUNTIME_DIR / "tasks" / project` (`task_ledger.py:62-63`)
- `role_memory.ROLE_MEMORY_DIR / project / ...` (`role_memory.py:30` + `role_memory_path()`)
- `_task_handoff_dir(project_ns)` → `RUNTIME_DIR / "tasks" / project_ns / <date>` (`orchestrator_text.py:509`)

ในขณะที่ issue สั่งให้ Brain ใช้ `encode_path_for_claude(cwd)` — เป็นคนละ namespace กันโดยสิ้นเชิง: `project_ns` เป็นชื่อที่ **ผู้ใช้ตั้งเอง** ตอนสร้างโปรเจกต์ (สั้น, human-chosen, เช่น `"agent-takkub"`) ส่วน `encode_path_for_claude` เป็นค่าที่ **derive จาก absolute path ของ cwd** (ยาว, deterministic, เช่น `"C--Users-monch-WebstormProjects-agent-takkub"` สำหรับ repo นี้ — วัดจริง 46 ตัวอักษร)

**ผลกระทบที่ต้องออกแบบรองรับ:**
1. โปรเจกต์เดียวกัน (`project_ns` เดิม) ที่ถูก assign จาก cwd คนละที่ (เช่น shared-tree ปกติ vs worktree isolation — ดู `isolation="worktree"` ที่สร้าง cwd ใหม่ทุกครั้ง `worktree_manager.py`) จะได้ `encode_path_for_claude(cwd)` **คนละค่ากัน** แม้ `project_ns` เดิม — ถ้า Brain เก็บ key ตาม cwd ตรงๆ ความรู้ของโปรเจกต์เดียวกันจะกระจายไปหลาย directory ตาม cwd ที่ใช้ assign แต่ละครั้ง ขัดกับเจตนา "Project Brain" ที่ต้องการรวมความรู้ต่อโปรเจกต์เดียว ไม่ใช่ต่อ cwd
2. **ข้อเสนอที่ต้องตัดสินใจใน Phase 1:** Brain ควรใช้ `encode_path_for_claude(<project root cwd ที่เสถียร>)` ไม่ใช่ `encode_path_for_claude(<cwd ที่ assign ครั้งนี้>)` — ต้องหา "cwd ที่เสถียรของ project_ns" มาก่อน (เช่น `default_cwd_for_role()` ที่เห็นใช้ที่ `orchestrator.py:1299, 1886` หรือ project registry's root path จาก `projects.json`) แล้ว encode ค่านั้นครั้งเดียว ไม่ใช่ encode cwd ของแต่ละ pane/worktree — มิฉะนั้น worktree-isolated shard ทุกตัวจะมี Brain directory แยกกันคนละที่ ผิดเจตนา "project scope" ที่ 06-SECURITY.md:49-51 ระบุไว้ (isolation: same project only)

### D.3 — Windows reserved characters

`encode_path_for_claude` แทนที่ **ทุก** ตัวอักษรที่ไม่ใช่ `[A-Za-z0-9]` ด้วย `-` (regex ที่ `token_meter.py:92` ยืนยันแล้ว) — เพราะฉะนั้น output รับประกันว่า**ไม่มี** ตัวอักษร Windows-reserved ทั้ง 9 ตัว (`< > : " / \ | ? *`) หลงเหลืออยู่เลย โดยไม่ต้องเพิ่ม guard พิเศษ — **นี่คือข้อดีของการบังคับใช้ `encode_path_for_claude` ที่ issue สั่งไว้** (เทียบกับถ้าใช้ path string ดิบๆ จะมีปัญหาแน่ๆ)

### D.4 — Windows reserved names (`CON`/`NUL`/`PRN`/`AUX`/`COM1-9`/`LPT1-9`)

**ตรวจแล้ว: `validate_name()` (`config.py:50-79`) ที่ระบบทั้งหมด (project_ns, role name) ใช้ตรวจสอบ — ไม่ได้กัน Windows reserved device name เลย** อ่าน regex ที่ใช้จริง: `_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")` (`config.py:17`) — โปรเจกต์หรือ role ที่ชื่อ `"con"`, `"nul"`, `"prn"`, `"com1"` ฯลฯ **ผ่านการ validate ได้ปกติ** (ทุกตัวเป็น alnum, ไม่มี `..`) — เป็นช่องว่างที่มีอยู่แล้วในระบบปัจจุบัน (ไม่ใช่ของใหม่จาก Brain) แต่ Brain ต้องรู้ก่อนออกแบบ path

**ทำไม Brain ปลอดภัยจากเคสนี้โดยบังเอิญ:** เพราะ D.1 บังคับใช้ `encode_path_for_claude(cwd)` (full resolved path ทั้งเส้น กลายเป็น string เดียวยาวๆ ที่ collapse separator ทั้งหมดด้วย `-`) แทน `project_ns` ตรงๆ — ผลลัพธ์จะไม่มีทางเท่ากับ bare `"con"`/`"nul"` เพราะเริ่มด้วย drive-letter prefix เสมอ (เช่น `"C--Users-..."`) ยาวกว่า 3-4 ตัวอักษรแน่นอน — **ข้อดีอีกข้อของการบังคับใช้ encode_path_for_claude**

**แต่จุดที่ยังเสี่ยงจริง:** ไฟล์ใต้ `continuations/<task-key>.json` — ถ้า `<task-key>` derive มาจาก **bare role name** ตรงๆ (เช่น role ชื่อ `"con"` ผ่าน validate_name ได้ตาม regex ข้างบน) ไฟล์ `con.json` จะ**สร้างไม่ได้บน Windows** (reserved name บล็อกทุกนามสกุล ไม่ใช่แค่ไม่มีนามสกุล) — **บทเรียนที่มีอยู่แล้วในโค้ดปัจจุบันที่ Brain ควรเลียนแบบ:** ทุกจุดสร้างชื่อไฟล์ต่อ role ในระบบวันนี้ **ไม่เคยใช้ bare role name เป็นชื่อไฟล์** — เสมอมี prefix ที่เป็นตัวเลข/timestamp นำหน้า: `f"{hhmmss}-{role}-ledger.md"` (`task_ledger.py:207`), `f"{HHMMSS}-{role_name}.md"` (`orchestrator_text.py:557`), `f"{HHMMSS}-{role_name}-subagent-{task_id[:8]}.md"` (`orchestrator.py:1334-1336`) — รูปแบบ `<timestamp>-<role>...` ทำให้ basename ไม่มีทางเท่ากับ reserved name เดี่ยวๆ ได้เลย (เพราะขึ้นต้นด้วยตัวเลข). **ข้อเสนอ:** `continuation.py`'s task-key ต้องตามรูปแบบเดียวกัน (มี prefix ที่ไม่ใช่ bare role/project name) — ห้ามตั้งชื่อไฟล์เป็น `<role>.json` หรือ `<project>.json` ตรงๆ

### D.5 — MAX_PATH 260 บน Windows

คำนวณจริงจาก path components ที่ยืนยันแล้ว สำหรับ dev checkout นี้ (`DATA_HOME == REPO_ROOT` ตาม `config.py:139-140` เพราะมี `pyproject.toml` + `src/` ที่ REPO_ROOT):

```
RUNTIME_DIR = C:\Users\monch\WebstormProjects\agent-takkub\runtime   (config.py:318, DATA_HOME/"runtime")
+ \brain\                                                             (7 ตัวอักษร)
+ C--Users-monch-WebstormProjects-agent-takkub                        (46 ตัวอักษร, วัดจริงจาก encode_path_for_claude บน repo นี้)
+ \continuations\                                                     (15 ตัวอักษร)
+ <task-key>.json                                                     (~37 ตัวอักษร ถ้าใช้ uuid hex 32 ตัว + prefix สั้น + ".json")
```
รวม: `46 (RUNTIME_DIR ก่อน runtime\) + 8 (runtime\) + 7 (brain\) + 46 + 15 + 37 ≈ 159 ตัวอักษร` — **ยังห่างจาก 260 พอสมควรสำหรับ repo นี้**

**แต่ความเสี่ยงจริงอยู่ที่ 2 กรณี:**
1. **Installed build** (ไม่ใช่ dev checkout): `_resolve_data_home()` (`config.py:120-144`) fallback เป็น `Path.home() / ".agent-takkub"` (บรรทัด 144) — สำหรับ corporate/domain profile ที่ home ลึกกว่านี้มาก (เช่น `C:\Users\firstname.lastname\Documents\...` หรือ redirected folder ที่พบได้ในองค์กร) ตัวเลขฐานจะสูงกว่า repo นี้มาก
2. **Worktree isolation:** cwd ของ pane isolation จะลึกกว่า project root เดิม (`.worktrees\wt-<role>-<timestamp>\...`) — ถ้า Brain project-key อิง cwd ของ worktree ตรงๆ (ตามที่ D.2 เตือนว่าไม่ควรทำ) ทั้ง encoded string จะยาวกว่านี้มาก เพราะรวม worktree path segment เข้าไปด้วย

**ไม่มีโค้ดจุดไหนในระบบปัจจุบันที่ guard `MAX_PATH` โดยเฉพาะ** (grep `260` ในบริบท path-length: ไม่พบ) — เป็นความเสี่ยงที่มีอยู่แล้วสำหรับ `RUNTIME_DIR/tasks/`, `RUNTIME_DIR/sessions/` ทุกวันนี้ด้วยเช่นกัน ไม่ใช่ปัญหาใหม่เฉพาะ Brain — แต่ Brain เพิ่ม path-depth อีกชั้น (`brain/<project>/continuations/`) จึงควรเป็นเฟสแรกที่เพิ่ม length-check + fallback (เช่น hash แทน task-key ยาวๆ) แทนที่จะรอให้เจอ production incident ก่อน

### D.6 — Concurrent append: cockpit + CLI เขียน `events.jsonl` พร้อมกัน — ต่างกันยังไงระหว่าง 2 OS

**ข้อเท็จจริงที่พิสูจน์แล้ว: วันนี้ระบบไม่มี "cockpit + CLI เขียนพร้อมกัน" เกิดขึ้นจริงสำหรับ event log** — ตรวจแล้ว `cli.py` **ไม่มี** `EVENTS_LOG`/`_log_event` เลย (grep ยืนยัน 0 match) เพราะ `cli-ipc-boundary` contract (`pyproject.toml:141-147`) บังคับให้ `cli.py` คุยกับ orchestrator ผ่าน TCP socket (`cli_server`) เท่านั้น — คำสั่ง `takkub assign`/`takkub done` ที่ผู้ใช้พิมพ์จาก terminal ไม่ได้เขียนไฟล์เอง แต่ส่ง request ผ่าน socket ให้ **กระบวนการ cockpit เดียว** เป็นคนเขียนไฟล์จริงเสมอ (ยืนยันจาก `_log_event` 3 จุดที่มีอยู่ — `lead_context.py:283`, `orchestrator_text.py:251`, `pipeline_executor.py:69` — ทั้งหมดอยู่ใน engine-layer modules ไม่ใช่ `cli.py`)

**ข้อยกเว้นที่มีอยู่จริง:** `disk-usage-layer` contract's comment (`pyproject.toml:307-310`) เขียนตรงๆ ว่า `disk_usage`/`worktree_manager` เป็นคำสั่งที่ `cli.py` **import ตรง (ไม่ผ่าน socket)** ด้วยเหตุผล "pure-local" — แปลว่ามีอย่างน้อย 2 โมดูล (`worktree_manager`, `disk_usage`) ที่ CLI process เขียนไฟล์ตรงๆ นอก orchestrator process จริง — **นี่คือ precedent ที่พิสูจน์ว่า multi-writer เป็นไปได้จริงถ้า Brain CLI (`brain remember`/`brain redact`) เลือกเส้นทางแบบเดียวกัน (direct filesystem write จาก bare CLI process) แทนที่จะผ่าน socket**

**Mechanism การเขียนปัจจุบัน (ไม่มี lock เลย, ยืนยันจาก `orchestrator_text.py:251-272`):** `_log_event()` เปิดไฟล์ด้วย `open(events_log, "a", encoding="utf-8")` ธรรมดา — **ไม่มี** `fcntl.flock`/`msvcrt.locking`/`filelock`/`portalocker` ที่ไหนในโค้ดเบสเลย (grep ยืนยัน 0 match ทั้ง repo) — เขียนบรรทัดเดียวสั้นๆ (JSON line) ต่อครั้ง, พึ่งพา OS-level atomic append-mode write ของทั้ง POSIX (`O_APPEND`) และ Windows (`FILE_APPEND_DATA`) ซึ่งรับประกัน atomicity เฉพาะ **single write() syscall ต่อครั้ง** ไม่ใช่ทั้ง read-modify-write cycle — ปลอดภัยสำหรับ "append บรรทัดสั้นๆ จากหลายโพรเซส" แต่ **ไม่ปลอดภัย**สำหรับการอ่าน-แก้ไข-เขียนทับไฟล์เดิม (เช่น `current-state.json` materialization ที่แผนต้องการ — ไฟล์นี้ไม่ใช่ append-only)

**ข้อเสนอสำหรับ Brain (Phase 1 `local_store.py`):**
1. `events.jsonl` (append-only) — เลียนแบบ `_log_event` pattern ตรงๆ ได้เลย (`open(path, "a")` ธรรมดา) **ถ้า** ทุก writer (ทั้ง cockpit engine และ CLI `brain remember`) เขียนบรรทัดสั้นๆ ครั้งละ 1 JSON line — ปลอดภัยพอทั้ง Windows/POSIX ตาม precedent ที่มีอยู่แล้ว
2. `current-state.json` (materialized, non-append) — **ต้องใช้ atomic-replace pattern ที่มีอยู่แล้ว** ไม่ใช่ open-append: `config._write_json_atomic()` (`config.py:82-94`) เขียนลง temp file ก่อนแล้ว `os.replace()` ทับ — มี retry-with-delay สำหรับ Windows AV-scanner race (`config.py:93-` เห็น comment "Windows can transiently reject replacement...") — Brain's `current-state.json` ควรใช้ pattern เดียวกันนี้ตรงๆ ไม่ต้องคิดใหม่
3. **ถ้า** Phase 2 เลือกให้ `takkub brain remember` เป็น direct-CLI-write (ตาม precedent worktree/disk_usage ข้อ D.6 บนสุด) แทนที่จะผ่าน socket — ต้องยอมรับว่าจะมี **true multi-process concurrent append** เกิดขึ้นจริงเป็นครั้งแรกสำหรับระบบนี้ (ไม่เหมือน events.log ที่ single-writer เสมอ) ซึ่ง append-only JSONL ยังปลอดภัยตามข้อ 1 แต่ `current-state.json` materialization (ถ้า trigger จาก CLI write ด้วย) จะต้องระวัง race ระหว่าง cockpit engine's materializer กับ CLI's materializer แย่งกันเขียนไฟล์เดียวกัน — **แนะนำให้ Phase 2 เดินตาม `cli-ipc-boundary` เดิม (route ผ่าน socket เข้า orchestrator) แทนการ direct-write แบบ disk_usage** เพื่อคง single-writer invariant ที่ระบบทั้งหมดพึ่งพาอยู่แล้วสำหรับ mutable state ที่ไม่ใช่ append-only

---

## สรุปสิ่งที่ Phase 1 ต้องตัดสินใจก่อนเขียนโค้ด (ไล่จาก audit นี้)

1. **A.1:** hook Brain ที่ใน `Orchestrator.assign()` (`orchestrator.py:1472`) ก่อนบรรทัด 1489 (เช็ค mode) — ไม่ใช่ duplicate logic ใน `_assign_dispatch`/`_register_subagent` แยกกัน
2. **A.2:** ถ้าแทรก Brain context เข้า pane-mode task string ต้องแทรกระหว่างบรรทัด 1780-1802 (หลัง verify-hint, ก่อน `_task_handoff_pointer`)
3. **A.3:** ต้อง mint task/continuation identity ใหม่เอง — ยืมจาก PaneState.task_id หรือ subagent task_id ไม่ได้ตรงๆ เพราะทั้งคู่ ephemeral และไม่ผูกกับ ledger
4. **A.11:** ต้องเพิ่ม import-linter contract ใหม่สำหรับ `agent_takkub.brain` (รูปแบบเดียวกับ `worktree-manager-leaf`) ก่อน Phase 1 เสร็จ ไม่ใช่ทีหลัง
5. **B:** ต้องเลือกกฎเขียนระหว่าง role_memory กับ Brain (ข้อ B.1) ก่อน Phase 5 ไม่งั้นความรู้จะซ้ำสองที่ถาวร
6. **C.4:** ใช้ budget หน่วย token (ตัวเลขแนะนำ: total 700-1,120 / continuation ≤500 / constraints ≤350 / lessons ≤250) วัดจาก payload จริงด้วย cl100k ที่ Phase 4 ไม่ใช่ chars-ceiling
7. **D.2:** ต้องตัดสินใจว่า Brain project-key encode จาก cwd ไหน (project root ที่เสถียร ไม่ใช่ per-worktree cwd) ก่อนเขียน `paths.py`
8. **D.4:** task-key ต้องมี prefix กัน bare-reserved-name เหมือน pattern ที่มีอยู่แล้วทั้งระบบ
9. **D.6:** Phase 2 CLI ควรผ่าน socket เข้า orchestrator (คง single-writer invariant) ไม่ควร direct-write แบบ disk_usage/worktree_manager — ต่างจาก precedent ที่มีอยู่ตรงๆ ต้องตัดสินใจชัดเจนไม่ใช่เดินตาม precedent โดยอัตโนมัติ

---

# ภาคผนวก — re-verification ที่ HEAD ปัจจุบัน (2026-08-18)

> audit ข้างบนถูกเขียนที่ baseline `0aee262` / **1.0.68** ตั้งแต่นั้น main ขยับไป 4 patch release
> ภาคผนวกนี้รันการตรวจซ้ำทุก `file:line` ที่เอกสารข้างบนอ้าง เทียบกับ HEAD ปัจจุบัน แล้วบันทึกว่าอันไหนยังตรง อันไหน drift
>
> **HEAD ที่ตรวจ:** `ea6feb7a5d56f21b0894f4f0bea14ae5a5afa2c9` · `pyproject.toml:3` → **1.0.72**

## AP.1 ผลตรวจ `file:line` ทุกจุดที่เอกสารข้างบนอ้าง

ตรวจด้วยการอ่านบรรทัดนั้นจริงแล้วแมตช์กับ symbol ที่เอกสารบอกว่าอยู่ตรงนั้น (ไม่ใช่แค่เช็คว่าไฟล์ยังอยู่)

| citation เดิม | สถานะที่ HEAD 1.0.72 |
|---|---|
| `digest_facts.py:29` `class DigestFacts` | ✅ ยังตรง |
| `orchestrator.subagent_done():1397` | ❌ **drift → `orchestrator.py:1436`** |
| `config.py:318` `RUNTIME_DIR` | ✅ ยังตรง |
| `task_ledger.py:62` `_ledger_dir` | ✅ ยังตรง |
| `task_ledger.py:207` `f"{hhmmss}-{role}-ledger.md"` | ✅ ยังตรง |
| `role_memory.py:30` `ROLE_MEMORY_DIR` | ✅ ยังตรง |
| `orchestrator_text.py:509` `_task_handoff_dir` | ❌ **drift → `orchestrator_text.py:500`** |
| `orchestrator_text.py:251` `_log_event` | ✅ ยังตรง |
| `orchestrator_text.py:557` `f"{HHMMSS}-{role_name}.md"` | ✅ ยังตรง |
| `token_meter.py:92` `_NON_ALNUM_RE` · `:95` `encode_path_for_claude` | ✅ ยังตรงทั้งคู่ |
| `config.py:17` `_SAFE_NAME` · `:50` `validate_name` | ✅ ยังตรงทั้งคู่ |
| `lead_context.py:283` · `pipeline_executor.py:69` `_log_event` proxies | ✅ ยังตรงทั้งคู่ |
| `pyproject.toml:141` `cli-ipc-boundary` | ✅ ยังตรง |

**สรุป:** 2 จาก 15 จุด drift ที่เหลือยังใช้อ้างอิงได้ ข้อสรุปเชิงสถาปัตยกรรมของ audit ข้างบน **ไม่มีข้อไหนล้ม**จากการขยับ 4 release นี้

### ตำแหน่งที่อัปเดตแล้วของ hook หลัก (ใช้ชุดนี้แทนช่วงบรรทัดในเอกสารข้างบน)

| hook | HEAD 1.0.72 |
|---|---|
| `assign()` public entry | `orchestrator.py:1511` |
| แตกไป subagent | `orchestrator.py:1537` |
| `_register_subagent()` | `orchestrator.py:1300` |
| `_assign_dispatch()` (boundary ร่วมของ pane + worktree) | `orchestrator.py:1744` |
| เข้า dispatch จาก normal path · worktree path | `orchestrator.py:1679` · `:2098` |
| `done()` | `orchestrator.py:3634` |
| `subagent_done()` | `orchestrator.py:1436` |
| `_compute_digest_facts()` | `orchestrator.py:3442` |
| ledger write ตอน assign (pane · subagent) | `orchestrator.py:1926` · `:1399` |
| ledger flip ตอน done (pane · subagent · close) | `orchestrator.py:3757` · `:1454` · `:2866` |
| role memory inject ตอน spawn | `spawn_engine.py:2079` · `:2106` |
| role memory write ตอน done ที่ fail | `orchestrator.py:3824` |
| vault / knowledge base write | `orchestrator.py:4284` · `:4299` · `:4389` |

### ลำดับการแปลง task ที่ยืนยันซ้ำ (ทั้งหมดใน `_assign_dispatch`)

| # | สิ่งที่เกิด | HEAD 1.0.72 |
|---|---|---|
| 1 | เก็บ `raw_task_for_ledger` (ledger บันทึกของดิบ ก่อนกลไก delivery) | `orchestrator.py:1772` |
| 2 | `_apply_session_goal` | `orchestrator.py:1773` |
| 3 | `_rewrite_task_for_codex` (provider-specific) | `orchestrator.py:1818` |
| 4 | `_append_verify_fail_hint` | `orchestrator.py:1819` |
| 5 | wrap planner / shard | `orchestrator.py:1825` · `:1831` |
| 6 | `_task_handoff_pointer` (task ย้ายลงไฟล์ เหลือ pointer) | `orchestrator.py:1841` |

**จุดแทรก Brain block ที่ถูกต้อง = ระหว่าง 2 กับ 3** — หลัง session goal (Brain จะได้เห็น goal) แต่ก่อน provider rewrite (ข้อ 3 ผูกกับ codex; Brain ต้อง provider-neutral ตาม #103) หลังข้อ 6 แทรกไม่ได้เพราะ task ไม่ได้อยู่ใน prompt แล้ว

## AP.2 สิ่งที่ต้องแก้ในข้อสรุปเดิม — 2 ข้อ

### AP.2.1 A.1 เดิมสรุปว่า `_assign_dispatch` คือ common boundary — **ไม่ครอบ subagent**

ตรวจซ้ำที่ HEAD นี้: `assign()` แตกไป `_register_subagent()` ที่ `orchestrator.py:1537` **ก่อน**ถึง `_assign_dispatch()` (`:1744`) และไม่มี call จาก `_register_subagent` กลับเข้า dispatch

`_assign_dispatch` ถูกเรียกจาก 2 ที่เท่านั้น — `orchestrator.py:1679` (normal) และ `:2098` (worktree) ทั้งคู่เป็น pane path

**สิ่งเดียวที่ทั้ง pane และ subagent เดินผ่านร่วมกันจริงคือ ledger API:** `create_assignment()` (เรียกที่ `:1926` และ `:1399`) กับ `mark_done()` (`:3757` และ `:1454`)

ผลต่อ Phase 1 — hook เดียวที่ครอบทั้งสองทางมี 2 ทางเลือก:
1. hook ที่ `task_ledger.create_assignment` / `mark_done` — จุดร่วมที่มีอยู่แล้ว แต่ทำให้ leaf store กลายเป็นจุดเชื่อม ต้องเช็ค `leaf-modules-pure` ก่อน
2. สร้าง boundary ใหม่ใน `assign()` **ก่อน**บรรทัด `:1537` — สะอาดกว่าและตรงเจตนา Rule 1 มากกว่า

### AP.2.2 D.4 เดิมสรุปว่า Brain "ปลอดภัยจาก reserved name โดยบังเอิญ" — จริงเฉพาะถ้าเลือก `encode_path_for_claude` เท่านั้น

ข้อสรุปเดิมถูกในเงื่อนไขของมัน แต่ยังไม่ได้ตรวจ `_safe()` ของ `role_memory` ซึ่งเป็นตัวเลือกที่ D.2 เปิดประเด็นว่าอาจเหมาะกว่า (เพราะ key ด้วย `project_ns` เหมือนระบบอื่นทั้งหมด)

รัน `role_memory._safe()` (`role_memory.py:105`) จริงกับ input ชุดทดสอบ:

| input | output | ประเมิน |
|---|---|---|
| `agent-takkub` | `agent-takkub` | ok |
| `my_app.web` | `my_app.web` | ok |
| `a:b<c>d\|e?f*g` | `a_b_c_d_e_f_g` | ok — reserved **chars** ครอบคลุม |
| `../escape` · `..` | `__escape` · `_` | ok — traversal กันได้ |
| `CON` · `NUL` | `CON` · `NUL` | ❌ reserved **name** ไม่ถูกกัน |
| `โปรเจกต์ไทย` | `___________` | ❌ **ชื่อไทยทั้งชื่อกลายเป็น underscore ล้วน** |
| `"a"*300` | ยาว 300 ไม่ถูกตัด | ❌ ไม่มี length cap |

**บรรทัดที่ต้องเน้น:** `_safe()` แปลงอักขระที่ไม่ใช่ `[A-Za-z0-9._-]` เป็น `_` ทีละตัว ดังนั้นชื่อโปรเจกต์ภาษาไทย**ทุกชื่อ**จะกลายเป็น underscore ล้วน และ**สองโปรเจกต์ไทยที่ยาวเท่ากันจะได้ path เดียวกัน**

นี่ไม่ใช่ความเสี่ยงของ Brain ในอนาคต — เป็นบั๊กที่มีผลกับ `role-memory` **วันนี้** (`role_memory.py:119` `role_memory_path`) และควรแยกเป็น issue ต่างหาก ไม่ใช่รวมเข้า Phase 1

ผลต่อการตัดสินใจ D.2: ถ้า Phase 1 เลือก key ด้วย `project_ns` (แทน `encode_path_for_claude`) **ห้ามใช้ `_safe()` ตามที่เป็นอยู่** ต้องแก้ให้กัน reserved name + มี length cap + ไม่ collapse non-ASCII ทั้งชื่อก่อน

## AP.3 ข้อมูลวัดเพิ่มที่ audit เดิมไม่มี

### AP.3.1 corpus-level token ratio (เดิมวัดจาก role-memory ไฟล์เดียว)

audit เดิมใช้ `runtime/role-memory/agent-takkub/backend.md` ไฟล์เดียว ได้ 3.573 chars/token วัดซ้ำแบบ corpus ทั้ง store:

| corpus | chars | tokens | chars/token |
|---|---:|---:|---:|
| **role-memory ทั้ง store — 75 ไฟล์** | 433,309 | 148,791 | **2.91** |
| `docs/lead/role-and-workflow.md` | 14,333 | 7,048 | 2.03 |
| root `CLAUDE.md` | 2,108 | 966 | 2.18 |
| control: อังกฤษล้วน | 3,599 | 800 | 4.50 |
| control: ไทยล้วน | 4,239 | 3,839 | 1.10 |

ไฟล์เดียวที่ audit เดิมเลือก (3.573) **ดีกว่าค่าเฉลี่ยจริงของ store ราว 23%** — ตัวเลขที่ควรใช้ตั้ง budget คือ **2.91** ไม่ใช่ 3.573

ที่ budget 4,000 chars: EN-pure 889 tok · **corpus จริง 1,375 tok** · TH-pure 3,636 tok — ช่วงกว้าง **4.1 เท่า** ยืนยันข้อสรุปเดิมว่าหน่วย chars ใช้ไม่ได้ และตอกย้ำว่าเพดานต้องเป็น token

เทียบกับกำไร #267 (5,780 tok/spawn): 4,000 chars แบบไทยล้วน = คืนกลับ **63%** ของที่ประหยัดมา

**ข้อเสนอตัวเลข:** เพดาน **1,200 tokens** ต่อ Brain block (≈ 21% ของกำไร #267 · ≈ 3,500 chars แบบ role-memory จริง · ≈ 1,300 chars ถ้าไทยล้วน) และ **measurement gate ต้องอยู่ที่ Phase 4** (จุดแรกที่ block เข้า prompt จริง) ไม่ใช่ Phase 8

### AP.3.2 MAX_PATH — วัดจริงบนเครื่องอ้างอิง

```
RUNTIME_DIR (dev)              = C:\Users\monch\WebstormProjects\agent-takkub\runtime   (52 chars)
ตัวอย่าง path Brain แบบเต็ม     = <RUNTIME_DIR>\brain\<project>\continuations\<task-key>.json
                                 ยาว 188 chars -> headroom ถึง 260 เหลือ 72 chars
LongPathsEnabled (HKLM\SYSTEM\CurrentControlSet\Control\FileSystem) = 1
```

- เครื่องนี้เปิด long path แล้ว แต่**ห้ามพึ่ง** — เป็นค่า per-machine และยังต้องมี manifest ฝั่ง Python ด้วย
- headroom 72 chars หมดทันทีถ้า task-key เป็น encoded cwd (`C--Users-monch-WebstormProjects-agent-takkub` ยาว 46 เอง) → **task-key ต้องเป็น hash สั้น** ตรงกับข้อเสนอ D.5 เดิม ตอนนี้มีตัวเลขรองรับแล้ว
- prod install path สั้นกว่า (`C:\Users\<u>\.agent-takkub\runtime` = 40 chars) → **dev คือเคสที่แคบกว่า ใช้ dev เป็นเกณฑ์**

### AP.3.3 ที่เก็บที่ใช้ชื่อ project เป็น path segment — ครบ 3 ที่ ไม่ตรงกันสักคู่

D.2 เดิมระบุไว้ 3 ที่แล้ว ภาคผนวกนี้เพิ่มมิติที่ยังไม่มี: **ระดับการ sanitize ที่ต่างกัน**

| ที่ | file:line | สร้าง path | sanitize |
|---|---|---|---|
| `task_ledger` | `task_ledger.py:62` | `RUNTIME_DIR / "tasks" / project` | **ไม่มีเลย** |
| `role_memory` | `role_memory.py:119` | `ROLE_MEMORY_DIR / _safe(project) / ...` | `_safe()` (มีช่องโหว่ตาม AP.2.2) |
| `lead_context` | `lead_context.py:759` | `RUNTIME_DIR / f"lead-guard-{project}.json"` | **ไม่มีเลย** — อยู่ใน filename |

ยืนยันบนดิสก์: ทั้ง `runtime/tasks/` และ `runtime/role-memory/` มีโฟลเดอร์ชื่อโปรเจกต์จริงอยู่แล้ว

**ผลต่อ Phase 1:** Brain เป็นรายที่ 4 ไม่ใช่รายแรก งานที่แท้จริงคือ**ทำให้ทั้ง 4 ใช้ตัวเดียวกัน** ไม่ใช่คิดวิธีที่ 4 ขึ้นมาเพิ่ม

## AP.4 เครื่องมือที่ใช้วัด

`tiktoken` (cl100k_base) ถูกติดตั้งใน `.venv` เพื่อการวัดรอบนี้ **ไม่ได้**เพิ่มเข้า `pyproject.toml` — ตรงกับข้อสังเกตในหัวข้อ C ของ audit เดิม ถ้า Phase 4 gate ต้องใช้ประจำ ต้องเพิ่มเป็น dev-dependency พร้อมเหตุผลกำกับ

## AP.5 สรุปสิ่งที่ Phase 1 ต้องรู้ (รวมของเดิม + ที่เพิ่มรอบนี้)

1. **ไม่มี common assign boundary** — subagent แตกทางที่ `orchestrator.py:1537` ก่อนถึง `_assign_dispatch` ต้องสร้างใหม่ หรือ hook ที่ ledger API (AP.2.1)
2. **ไม่มี task_id** — ledger identity คือ `(project, role, open row)` (`task_ledger.py:290` `_resolve_open_row`) ไม่ใช่ key ที่อ้างอิงได้จากภายนอก
3. **budget ต้องเป็น token ไม่ใช่ chars** — corpus จริง 2.91 · ไทยล้วน 1.10 · ช่วงกว้าง 4.1 เท่า เสนอเพดาน 1,200 tok gate ที่ Phase 4 (AP.3.1)
4. **path identity มีอยู่แล้ว 3 แบบที่ไม่ตรงกัน** — งานคือรวมให้เหลือหนึ่ง (AP.3.3)
5. **`_safe()` มีบั๊กที่กระทบวันนี้** — ชื่อโปรเจกต์ภาษาไทยชนกันใน `role-memory` อยู่แล้ว ควรแยก issue (AP.2.2)
6. **role memory inject ที่ spawn (`spawn_engine.py:2079`) ไม่ใช่ที่ assign** — ถ้า Brain inject ที่ assign จะคนละจังหวะกัน pane ที่ถูก assign ซ้ำโดยไม่ respawn จะได้ Brain block ใหม่แต่ role memory เดิม
7. **จุดแทรก Brain block = ระหว่าง `_apply_session_goal` กับ `_rewrite_task_for_codex`** (`orchestrator.py:1773` → `:1818`)
