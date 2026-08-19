# Core V2 — Phase 9: Settings UI (epic #309)

> worktree `wt/frontend-1787130627`, base `feat/v2-core` (Phase 1–8a merged) · 2026-08-19

## สรุป

เพิ่ม sidebar section ใหม่ **"CORE V2"** เข้า `src/agent_takkub/settings_window.py`
(ตัวที่ wired จริงใน app — `settings_management/` เป็น dev-only harness ยังไม่มี
in-app entry point, ตรวจสอบแล้วผ่าน grep ก่อนเริ่ม) 6 หน้า: **Overview / Accounts
& Pools / Routing / Brain / Scheduler / Migration** ทุกหน้า read-mostly, เปิดได้
แม้ core store ว่าง/flag ปิด, ไม่มี I/O บน main thread สำหรับ operation ที่ block
จริง (Brain recall/reindex, Migration inspect/plan/dry-run — thread), ไม่มีปุ่ม
migration apply ตามสั่ง.

Widget-building logic อยู่ใน **`src/agent_takkub/settings_core_v2.py`** (ไฟล์
ใหม่ — mixin `CoreV2SettingsMixin`) แทนที่จะเติมเข้า `settings_window.py` ตรงๆ
(อยู่แล้ว 3875 บรรทัด) — pattern เดียวกับ `user_actions.UserActionsMixin`/
`project_wizard.ProjectWizardMixin` ที่ mix เข้า `MainWindow` (ดู godfile-map.md);
`settings_window.SettingsWindow(QDialog, CoreV2SettingsMixin)` ประกอบเข้าด้วยกัน
ที่ `VIEW_CORE_V2_OVERVIEW..VIEW_CORE_V2_MIGRATION = 11..16` (append ต่อจาก
Performance ไม่ renumber ของเดิม ตาม convention ในไฟล์).

## Flag config-fallback (`core/*/flag.py`)

`TAKKUB_V2_ROUTER`/`_CONVERSATION`/`_BRAIN`/`_SCHEDULER` ทั้ง 4 flag.py แก้ให้
**env ชนะเสมอ, unset ถึงจะ fallback ไปอ่าน config ที่ persist ไว้**:

```python
raw = os.environ.get("TAKKUB_V2_ROUTER")
if raw is not None:
    return raw == "1"
from agent_takkub import core_v2_settings
return core_v2_settings.flag_enabled("router")
```

`TAKKUB_V2_CONTEXT` **ไม่มี core module ให้แก้** — `core.context`/Context Builder
เป็น phase 7c ที่ยังไม่ถูกสร้าง (ตรวจแล้วจาก `core.brain.facade`'s docstring เอง:
"7c Context Builder is the first real caller" ของ Second Brain แต่ 7c เองไม่มี
โค้ด) Overview ยังโชว์แถว toggle นี้ (เพื่อให้สอดคล้องกับ task ที่ระบุ 5 flag) แต่
**disabled + tooltip "ยังไม่มี core module ให้ toggle นี้เชื่อมถึง"** — ไม่เสก
plumbing ปลอมที่ไม่มีอะไรมาอ่านจริง.

Config เก็บที่ **`src/agent_takkub/core_v2_settings.py`** (ไฟล์ใหม่ — ไม่มี Qt,
ไม่มี `agent_takkub.core` dependency, pattern เดียวกับ `performance_settings.py`:
dataclass + `path()`/`load()`/`save()`) เขียนที่ `config.SETTINGS_HOME/
core-v2-settings.json`. เก็บ 2 อย่าง: `flags` dict (5 ตัว) และ
`SchedulerPolicyConfig` (SlotPolicy ไม่มี persistence layer มาก่อนเลยในโค้ดที่มีอยู่
— `resource_governor.ResourceGovernor.__init__` รับ `slot_policy` ที่สร้างใน memory
เท่านั้น ไม่มี env var/config file ใดๆ ตรวจแล้วจาก `docs/v2/phase8a-report.md`
บรรทัด "ไม่ได้เติม env var สำหรับ SlotPolicy").

**Scope ของไฟล์นี้ไม่ตรง `core/*/flag.py` หรือ `settings_management/` เป๊ะ** —
เป็น top-level `agent_takkub/` module (เหมือน `performance_settings.py` ที่
`settings_window.py` import ใช้อยู่แล้ว) จำเป็นสำหรับให้ flag fallback + Scheduler
editor มีที่เก็บจริง ไม่ใช่ core business logic — ตรวจ `pyproject.toml`'s
import-linter แล้ว: `core-is-bottom-layer` ห้ามเฉพาะสิ่งที่ `agent_takkub.core`
import (ไม่ใช่ใครมา import core), และ `core.storage.paths` เองก็ import
`agent_takkub.config` ตรงๆ อยู่แล้ว (precedent) — `lint-imports` รัน 28 contracts
ผ่านหมดหลังแก้ (ดูท้ายไฟล์).

## หน้า Settings ทั้ง 6

1. **Overview** — toggle ทั้ง 5 flag (ToggleSwitch, ค่าเริ่มจาก
   `core_v2_settings.load()`) + ปุ่ม "Save flags" แยกจาก footer Save & Apply
   ของ SettingsWindow (เหตุผลด้านล่าง) + summary `version.json`
   (`core.versioning.store.read_version_doc()`, empty state ชัดถ้ายังไม่มี record)
2. **Accounts & Pools** — list `ProviderAccount`/`AccountPool` จาก
   `core.accounts.registry` (JSONL append-only, อ่านสดทุกครั้ง ไม่ cache) +
   add/edit/remove ผ่าน dialog (`_AccountEditDialog`/`_PoolEditDialog`) เขียน
   ทันทีเหมือน pattern เดิมของ Templates' Duplicate/Delete — **ไม่มี field
   credential เลย มีแค่ `secret_ref` string ตามสั่ง**
3. **Routing** — role picker (`roles.all_role_names(include_lead=True)`) →
   `core.routing.facade.effective_provider_for_v2(role, project)` (เคารพ flag
   จริง ไม่บังคับ v2) + preview account ที่ pool จะเลือกผ่าน
   `core.accounts.selector.selector_for(strategy)` — read-only ล้วน ไม่มี Save
4. **Brain** — จำนวน memory ต่อ scope/trust (`BrainStore(project).load_active()`
   บวก `_global` bucket) + search ผ่าน `core.brain.facade.recall()` (thread) +
   ปุ่ม "Reindex" (thread) — **หมายเหตุ**: `RetrievalEngine` ไม่มี persistent
   index จริง (`recall()` rescans ทุกครั้งอยู่แล้ว ตาม docstring ของมันเอง) ปุ่มนี้
   จึงแปลว่า "บังคับอ่านจากดิสก์ใหม่ off main thread" ไม่ใช่ rebuild index จริง —
   ไม่ได้เสกฟีเจอร์ที่ไม่มีอยู่
5. **Scheduler** — `SlotPolicy` editor (global agents/panes spin + provider/
   account/project dict ผ่าน `id=จำนวน` text editor ต่อบรรทัด) + default
   priority combo + ปุ่ม Save แยก (เขียนลง `core_v2_settings`) + panel
   "Backpressure (estimate)" — **ไม่ใช่ live state จริงของ orchestrator** (คุย
   ละเอียดด้านล่าง)
6. **Migration** — ปุ่ม "Refresh (inspect + plan)" + "Dry-run" (ทั้งคู่ thread,
   เรียก `core.migration.engine.MigrationEngine().inspect()/plan()/dry_run()`)
   แสดง `StepReport` list เป็น mono text — **ไม่มีปุ่ม apply ในหน้านี้เลย**
   (มี unit test เฉพาะ scoped ที่ view นี้ยืนยัน)

## การตัดสินใจสำคัญ

- **ไม่ผูกกับ footer "Save & Apply" เดิม** — transaction นั้น snapshot+rollback
  7+ store อยู่แล้ว (`_on_save_apply_clicked`) เอา 6 หน้าใหม่ (JSONL registry /
  thread report / flag set ที่ shape ไม่เหมือนกันเลย) ยัดเข้าไปจะทำให้ rollback
  surface ใหญ่ขึ้นแบบไม่มีประโยชน์จริง — แต่ละหน้าเขียนผ่านปุ่มของตัวเอง
  (Accounts/Pools = ทันทีเหมือน Templates, Overview/Scheduler = ปุ่ม Save
  แยก) แทน
- **Brain/Migration ไม่ auto-fetch ตอนเปิดหน้า** — เดิมจะยิง thread ทันทีตอน build
  view แต่ `SettingsWindow()` ถูก construct ใหม่ทุกครั้งที่เปิด (ตาม class
  docstring เอง) แปลว่าทุก dialog ที่เปิดจะยิง background thread แม้ไม่เคยเข้าหน้า
  Brain/Migration เลย — เสี่ยง flaky ในชุด test เดิมทั้งหมดที่ construct
  SettingsWindow (100+ จุด) เปลี่ยนเป็น lazy-load ตอนกดปุ่มแทน (spec เดิมก็ขอแค่
  "ปุ่ม...thread" ไม่ได้ขอ auto-load)
- **Backpressure panel เป็น estimate ไม่ใช่ live state** — `classify()` ต้องการ
  `overloaded` latch + `queued_count`/`active_capacity_hint` ซึ่งเป็น in-memory
  state ของ `ResourceGovernor` ที่กำลังรันอยู่บน orchestrator เท่านั้น
  `settings_window.py` ประกาศชัดในหัวไฟล์ตัวเองว่า "MUST NOT import app or cli"
  — เข้าไม่ถึง live governor ได้จริงๆ ไม่ใช่แค่เลือกไม่เข้า จึงโชว์แค่ CPU/RAM
  sample สดจาก `psutil` + threshold จาก `performance_settings.load()` แล้ว
  classify แบบ `overloaded=False` (เพราะไม่รู้จริง) — label ชัดว่า "ประมาณการ...
  ไม่ใช่สถานะ live"
- **Icon นำของเดิมมาใช้ซ้ำ** — `static/icons/nav/` มีแค่ 6 ชื่อ (diamond/grid/
  pipeline/star/target/user) ไม่ได้เพิ่ม asset ใหม่ (ponytail) — Overview=target,
  Accounts=user, Routing=pipeline, Brain=star, Scheduler=grid, Migration=diamond

## Provider-neutral

`_cv2_provider_ids()` อ่านจาก `provider_spec.PROVIDER_REGISTRY.keys()` (ไม่
hardcode ชื่อ provider ใดๆ) ใช้เป็น dropdown ทั้งใน Account/Pool dialog —
ครอบทุก provider ที่ registry รู้จัก (claude/codex/gemini/opencode/kimi/cursor
+ อนาคต) ตาม multi-provider policy

## Tests

- `tests/test_settings_core_v2.py` (ใหม่, 21 tests): `core_v2_settings`
  round-trip (flags/scheduler policy/corrupt-file fallback), flag
  config-fallback (env ชนะ config) ต่อ router/brain/scheduler/conversation,
  ทั้ง 6 view สร้างได้สำเร็จตอน store ว่างทั้งหมด, Overview
  toggle-seed-from-disk + save, Accounts/Pools list+remove round-trip,
  Routing resolves จริง, Brain reindex thread, Scheduler policy save
  round-trip, Migration refresh thread + ไม่มีปุ่ม apply
- `tests/test_settings_window.py` — เพิ่ม `config.SETTINGS_HOME`/
  `config.RUNTIME_DIR` เข้า autouse isolation fixture (ทุก `SettingsWindow()`
  ตอนนี้ build Core V2 views ด้วย ต้อง isolate ไม่ให้แตะ `~/.takkub` จริง) +
  แก้ `test_has_eleven_stacked_views` → `test_has_seventeen_stacked_views`
  (17 = 10 เดิม + Performance + Core V2 6 หน้า)

รันแล้วผ่านหมด (targeted, ไม่ใช่ full suite ตาม policy):

```
tests/test_settings_core_v2.py ............ 21 passed
tests/test_settings_window.py .............. 103 passed
tests/test_core_routing.py + test_core_conversation.py + test_core_brain_adapter.py
  + test_core_scheduling.py + test_resource_governor_scheduling.py ........ 87 passed
lint-imports: 28 contracts kept, 0 broken
ruff check: All checks passed
```

## Screenshot (offscreen, `QT_QPA_PLATFORM=offscreen`, `QWidget.grab()`)

เก็บที่ `runtime/exports/`:

- `phase9-core-v2-overview.png`
- `phase9-core-v2-accounts.png`
- `phase9-core-v2-routing.png`
- `phase9-core-v2-brain.png`
- `phase9-core-v2-scheduler.png`
- `phase9-core-v2-migration.png`

Self-review ตาม `cockpit-ui-style` checklist: ทุก color/font/radius ผ่าน
`cockpit_theme` token (ไม่มี hex literal ใหม่ใน `settings_core_v2.py`), ปุ่ม
primary ใช้ `gold_button` เดียว, chip/badge ใช้ font mono, panel ใช้
`GROUND_PANEL`/`BORDER_HAIRLINE` ผ่าน objectName `panel`/`panelAlt`/
`providerRow` เดิม (ไม่เพิ่ม QSS class ใหม่), empty state มีข้อความชัดเจนทุกหน้า
(ตรวจ Accounts/Brain/Migration/version.json ว่าง) พบจุดเดียวที่แก้ระหว่าง
self-review: Accounts/Pools list สูงเกินไปตอนมีแค่ 1 แถว (`setMaximumHeight(160)`
แก้แล้ว, screenshot ที่แนบคือหลังแก้).

## Gap ที่รู้ (ไม่เงียบ)

- `TAKKUB_V2_CONTEXT` ไม่มี core module จริง (ดูด้านบน)
- Backpressure panel เป็น estimate ไม่ใช่ live orchestrator state (ดูด้านบน)
- Scheduler policy ที่บันทึกใน UI **ยังไม่ถูก wire เข้า `resource_governor`
  จริง** — task นี้ขอบเขตแค่ "Settings UI" (`แก้เฉพาะ UI/settings_management +
  core/*/flag.py`) การ wire `ResourceGovernor` ให้อ่าน `core_v2_settings`
  policy ตอน boot เป็นงานคนละ scope (แตะ `resource_governor.py`/engine
  boot path ซึ่งไม่อยู่ใน allowlist ของ task นี้) — ธง gap นี้ไว้ให้ Lead ตัดสินใจ
  ว่าเป็น phase ถัดไปหรือไม่
