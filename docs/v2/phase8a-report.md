# Core V2 — Phase 8a: Scheduler / Resource extend (epic #309)

> worktree `wt/backend-2-1787129556`, base `feat/v2-core` (Phase 1–7ab merged) · 2026-08-19

## สรุป

เพิ่ม `src/agent_takkub/core/scheduling/` (**NEW**, pure — ไม่มี case ตรง
`REUSE_VS_REWRITE_MATRIX.md` เพราะ scheduling-policy vocabulary เป็นชั้นที่
ว่างเปล่ามาก่อน) แล้ว **EXTEND** `src/agent_takkub/resource_governor.py` (§5: "EXTEND — มี
admission + fair queue + resource class แล้ว · เติม dimension: provider
slot, account slot, project slot, priority, GPU") ให้เรียกเข้า
`core.scheduling.facade` — จุดเชื่อมเดียวตาม plan §0 rule 4. `TAKKUB_V2_SCHEDULER`
off by default: `tests/test_resource_governor.py` เดิมทั้ง **22 ตัวผ่านโดยไม่แก้
แม้แต่บรรทัดเดียว** — พิสูจน์ parity ตามที่ task สั่ง.

GPU dimension **ไม่ได้ทำ** — ไม่มี GPU signal ใดๆ ในโค้ดเดิม (`system_baseline.py`/
`performance_settings.py` ไม่มี VRAM/GPU field) เพิ่มเข้าไปนอก scope คำสั่งนี้
โดยไม่ตรวจสอบ backend จริง (nvidia-smi/DirectX) จะเป็นแค่ mock — ประกาศเป็น gap
ด้านล่าง ไม่เงียบ.

## `src/agent_takkub/core/scheduling/` (pure, ไม่แตะ PyQt6/orchestrator/UI)

- **`flag.py`**: `v2_scheduler_enabled()` อ่าน `TAKKUB_V2_SCHEDULER` (เดียวกับ
  pattern `core.conversation.flag`/`core.routing.flag`)
- **`models.py`**: `Priority` (`IntEnum`, ต่ำ=ก่อน: `CRITICAL`/`HIGH`/`NORMAL`/
  `LOW`/`BACKGROUND`) · `BackpressureLevel` (`NORMAL`/`THROTTLE_NEW`/
  `PAUSE_BACKGROUND`/`QUEUE`) · `SlotPolicy` (ทุก dimension default
  `None`/`{}` = unlimited — all-defaults `SlotPolicy()` ไม่ deny อะไรเลย) ·
  `SlotRequest`/`ActiveCounts` (snapshot ที่ caller สร้างเอง — pure function
  ไม่ผูกกับวิธี `resource_governor` เก็บ token)
- **`policy.py`**: `evaluate(request, counts, slot_policy)` — เช็ค 6 มิติตาม
  §07 §4-7 (global maxAgents/maxPanes, provider maxConcurrent, account
  maxConcurrent, project maxAgents/maxPanes) คืน reason string หรือ `""`
- **`backpressure.py`**: ladder ตาม §10 (Normal → Throttle new → Pause
  background → Queue) ใช้ signal เดิมจาก `resource_governor.sample()`
  (cpu/ram/overloaded) — `THROTTLE_NEW` เข้าก่อน hard `overloaded` latch จริง
  (ที่ครึ่งทางระหว่าง resume/pause threshold) ไม่ kill agent ที่รันอยู่
  (`admits()` เกตแค่งานใหม่)
- **`priority_queue.py`**: `order_by_priority()` — stable sort ต่อยอด
  round-robin cursor เดิมของ `resource_governor` (ไม่ได้เขียน queue engine
  ใหม่ตามที่ REUSE_VS_REWRITE_MATRIX ไม่ได้สั่ง REPLACE)
- **`process_registry.py`**: `ProcessRegistry` — track
  pid/agent/project/task/pane/provider/account/startedAt/status ตาม §12-13,
  `detect_stale(is_alive)` = crash-recovery inspect ตาม §14 (inject
  liveness callable แทน import psutil/ctypes ตรง — ให้ caller ห่อ
  `job_object_manager`/PaneState read-only เอง ตามที่ task สั่ง)
- **`runtime_control.py`**: `AgentRuntimeControl` — `pause()`/`resume()` แค่
  flip flag (ไม่แตะ process จริง), `checkpoint()` เรียก injected
  `checkpoint_fn` เฉพาะ flag on (fail-open), `cancel()` เป็น terminal state
- **`facade.py`**: จุดเชื่อมเดียว — `extended_denial_reason`/
  `backpressure_level`/`backpressure_admits`/`order_projects` ทุกตัว flag
  off = no-op คืนค่าที่ทำให้ caller เหมือนเดิม, flag on = fail-open (exception
  ในนี้ไม่เคยกลายเป็นการ deny slot ที่ legacy check อนุญาตอยู่แล้ว)

## `src/agent_takkub/resource_governor.py` (EXTEND, backward-compat)

- `ResourceGovernor.__init__` เติม `slot_policy: SlotPolicy | None = None`
  keyword-only — ไม่ให้ = `SlotPolicy()` (unlimited)
- `ResourceToken` เติม `provider_id`/`account_id` (default `None`) ต่อท้าย ·
  `QueuedTask` เติม `provider_id`/`account_id`/`priority` (default
  `None`/`None`/`Priority.NORMAL`) ต่อท้าย — เขียนด้วย keyword เสมอ ไม่กระทบ
  field เดิม
- `request_slot()`/`enqueue()` เติม keyword-only param เดียวกัน 3 ตัว —
  caller เดิมที่ไม่ส่งมา ได้ default เป๊ะ
- `_denial_reason()`: legacy heavy/class/overload check **เหมือนเดิมทุก
  บรรทัด** เมื่อ resource_class เป็น `HEAVY`/`BROWSER`/`BUILD`/`TEST`/
  `PACKAGE_INSTALL` และ `LIGHT`/`NORMAL` ยัง return `""` ทันทีเหมือนเดิม —
  ต่อท้ายด้วย backpressure ladder check + `core.scheduling.facade.extended_denial_reason`
  ซึ่งทั้งคู่เป็น no-op เมื่อ flag off (พิสูจน์ด้วย
  `tests/test_resource_governor.py` เดิมผ่านครบ)
- `dispatch_waiting()`: เติม `scheduling_facade.order_projects()` หนึ่งบรรทัด
  ก่อนเข้า round-robin loop เดิม (flag off = คืน list เดิมไม่เรียง) และส่ง
  `provider_id`/`account_id`/`priority` จาก `QueuedTask` เข้า `request_slot`
- `_active_counts()` (ใหม่): แปลง token dict เดิมเป็น `ActiveCounts` snapshot
  — นับทุก token เข้า project (ไม่ใช่แค่ heavy class) เพราะ "project
  maxAgents/maxPanes" ใน §07 §7 เป็น cap รวม ไม่ผูก resource class

## `src/agent_takkub/core/models/account.py` (EXTEND, backward-compat)

- `AccountLimits` ใหม่ (`max_concurrent: int | None = None`) + `ProviderAccount`
  เติม field `limits: AccountLimits = field(default_factory=AccountLimits)`
  ต่อท้าย — ยืนยันแล้วว่า `tests/test_core_accounts.py`/`test_core_models.py`/
  `test_core_conversation.py` เดิมผ่านครบ (frozen dataclass, ทุก field เดิม
  ตำแหน่งเดิม, field ใหม่อยู่ท้ายสุดพร้อม default)

## Multi-provider / cross-platform

- ไม่มี path/command เฉพาะ platform ใน `src/agent_takkub/core/scheduling/*` — pure dataclass +
  stdlib เท่านั้น, ไม่มี `sys.platform` branch
- `provider_id`/`account_id` เป็น string ทั่วไป ไม่ผูก claude/codex/gemini
  เฉพาะเจาะจง — `SlotPolicy.provider_max_concurrent`/`account_max_concurrent`
  เป็น mapping เปิดกว้างสำหรับทุก provider (#103)
- `ProcessRegistry.detect_stale` inject `is_alive` callable แทนเรียก
  psutil/ctypes ตรง เพื่อไม่ผูก platform — caller (Windows ConPTY/macOS
  `_pty_backend`) เป็นผู้ห่อ liveness check ของ platform ตัวเอง (ยังไม่มี
  caller จริงวันนี้ — ดู Gap)

## ไฟล์ที่สร้าง/แก้

**สร้างใหม่** (9 module + 2 test file):
- `src/agent_takkub/core/scheduling/__init__.py`
- `src/agent_takkub/core/scheduling/flag.py`
- `src/agent_takkub/core/scheduling/models.py`
- `src/agent_takkub/core/scheduling/policy.py`
- `src/agent_takkub/core/scheduling/backpressure.py`
- `src/agent_takkub/core/scheduling/priority_queue.py`
- `src/agent_takkub/core/scheduling/process_registry.py`
- `src/agent_takkub/core/scheduling/runtime_control.py`
- `src/agent_takkub/core/scheduling/facade.py`
- `tests/test_core_scheduling.py`
- `tests/test_resource_governor_scheduling.py`

**แก้ไข**:
- `src/agent_takkub/resource_governor.py` (extend, backward-compat — ดู
  รายละเอียดด้านบน)
- `src/agent_takkub/core/models/account.py` (extend, backward-compat — เติม
  `AccountLimits` + `ProviderAccount.limits`)

## Test count

- ไฟล์ใหม่ 2 ไฟล์: **35 tests** (`test_core_scheduling.py` 27,
  `test_resource_governor_scheduling.py` 8)
- targeted battery รวม (ไฟล์ใหม่ + `test_resource_governor.py` เดิม [22, flag-off
  parity proof] + `test_core_accounts.py`/`test_core_models.py`/
  `test_core_conversation.py` [ตรวจ `ProviderAccount`/`AccountLimits` เดิมไม่พัง]):
  **154 passed, 0 failed**
- ไม่ได้รัน full suite ตามนโยบาย targeted-tests-only (qa batch gate จะรันเต็ม
  ก่อน merge)

## Lint-imports

`lint-imports` (28 contracts): **28 kept, 0 broken** — `core-is-bottom-layer`
ครอบ `agent_takkub.core` ทั้งต้นไม้อยู่แล้ว (`core.scheduling.*` ไม่ import
PyQt6/orchestrator/main_window/app/cli/cli_server/agent_pane/terminal_widget)
ไม่ต้องเพิ่ม contract ใหม่ — `src/agent_takkub/resource_governor.py` import ลงมาที่
`core.scheduling` ทิศทางเดียว (governor → core) ไม่มี cycle

## Ruff

`ruff check` บน `src/agent_takkub/core/scheduling/` + `src/agent_takkub/core/models/account.py`
+ `src/agent_takkub/resource_governor.py` + 2 test file ใหม่: **All checks passed**

## Gap / #103 (ประกาศชัด ไม่เงียบ)

1. **GPU/VRAM ไม่ได้ทำ** (§07 §3/§15 "Qwen Local VRAM 95% → ไม่ schedule
   local") — ไม่มี GPU signal ใดๆ ในโค้ดเดิมให้ต่อยอด (`system_baseline.py`
   ไม่มี field นี้) การเพิ่ม field เปล่าที่ไม่มี real backend จะเป็นแค่ mock
   ที่หลอกตัวเอง; upgrade path = ต้องมี GPU sampler จริงก่อน (nvidia-smi
   parse หรือเทียบเท่า) ค่อยเติม `SlotPolicy`/`BackpressureSignal` dimension
2. **`ProcessRegistry`/`AgentRuntimeControl` ยังไม่มี caller จริง** — ตามที่
   task สั่ง ("ยังไม่เชื่อม UI") มีแต่ pure class ที่ทดสอบผ่าน fixture
   liveness/checkpoint callable เอง; ยังไม่มี call site ใน orchestrator/
   job_object_manager วันนี้ — จุดเชื่อมจริง (wrap `job_object_manager`
   read-only + `core.conversation` checkpoint) เป็นงาน phase ถัดไป
3. **`ActiveCounts.agents_global`/`panes_global` เป็นตัวเลขเดียวกันเสมอจาก
   `resource_governor`** (`ponytail:` ระบุไว้ใน `models.py` แล้ว) — หนึ่ง
   token วันนี้ = หนึ่ง active pane = หนึ่ง active agent เสมอ ยังไม่มี caller
   ที่ต้องแยกสองมิตินี้จริง upgrade path = ส่งเลขต่างกันเข้า `ActiveCounts`
   เมื่อมี caller ที่ต้องการ ไม่ต้องแก้ shape
4. **`resource_governor`'s existing `GovernorLimits.from_environment()`
   ไม่ได้เติม env var สำหรับ `SlotPolicy`** — ตามสเปค task ("default =
   unlimited/ค่าเดิม") ตั้งใจเว้นไว้ ผู้เรียก (เช่น cockpit settings UI ใน
   phase ถัดไป) เป็นผู้สร้าง `SlotPolicy` เองแล้วส่งเข้า
   `ResourceGovernor(slot_policy=...)`
