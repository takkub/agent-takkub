# V2 Implementation Plan

> Phase 0 deliverable · baseline `9c6b56d` (1.0.75) · 2026-08-19
> คู่กับ [`CURRENT_ARCHITECTURE_AUDIT.md`](CURRENT_ARCHITECTURE_AUDIT.md) · [`REUSE_VS_REWRITE_MATRIX.md`](REUSE_VS_REWRITE_MATRIX.md)
>
> **สถานะ: รออนุมัติ** — blueprint สั่งไว้ว่า *"Do not start destructive refactoring until this report is reviewed."*

---

## 0. หลักการที่แผนนี้ยึด

1. **ไม่ big-bang** — Core V2 เป็น package ใหม่ที่วิ่งคู่ขนาน ของเดิมไม่แก้จนกว่า adapter จะพร้อม
2. **runnable ทุก phase** — จบแต่ละ phase ต้อง `pytest` เขียว + cockpit เปิดใช้งานได้จริง + ปล่อย release ได้ถ้าต้องการ
3. **feature flag ทุกจุดเชื่อม** — ปิด flag = พฤติกรรมเดิมเป๊ะ (proof = suite เดิมเขียวโดยไม่แก้ expected values)
4. **fail-open** — ชั้นใหม่พังต้องไม่บล็อก assign/done เด็ดขาด
5. **copy-never-move** — migration ห้ามลบ source จนกว่า validate ผ่าน
6. **ไม่มี I/O บน Qt main thread** — บทเรียน D8 (205 UI stalls / 24 ชม.)
7. **multi-provider first** — ทุก phase ต้องคิดถึง 6 provider หรือประกาศ gap เข้า #103

---

## 1. Proposed V2 Module Tree

blueprint เสนอ `src/core/…` แต่ repo เป็น package เดียว (`src/agent_takkub/`) และมี import-linter ผูกกับชื่อโมดูลอยู่แล้ว → วางเป็น sub-package:

```text
src/agent_takkub/core/
├── __init__.py
├── models/                    # Phase 1 — pure dataclasses, ไม่มี dependency
│   ├── provider.py            #   ProviderDefinition, ProviderFeature, Transport
│   ├── account.py             #   ProviderAccount, AccountPool, SelectionStrategy
│   ├── model.py               #   ModelDefinition, ModelProfile
│   ├── agent.py               #   AgentTemplate, AgentInstance
│   ├── task.py                #   Task, TaskState
│   ├── conversation.py        #   Conversation, ProviderSessionBinding, Checkpoint
│   ├── capability.py          #   Skill, MCPServer, Plugin, Tool, CapabilityScope
│   ├── memory.py              #   MemoryRecord, MemoryKind, Scope, Trust, Confidence
│   └── version.py             #   ComponentVersion, CompatibilityRule
│
├── contracts/                 # Phase 1 — typing.Protocol เท่านั้น (sync, ไม่ async)
│   ├── provider_adapter.py
│   ├── account_selector.py
│   ├── brain_adapter.py
│   ├── secret_manager.py
│   ├── store.py               #   ConfigStore / StateStore / RuntimeStore
│   └── migration.py
│
├── storage/                   # Phase 1 (โครง) → Phase 8 (ของจริง)
│   ├── paths.py               #   อ่านจาก config.py ตัวเดิม ไม่ประกาศ home ใหม่
│   ├── jsonl_store.py         #   append-only + atomic + corruption-tolerant
│   └── legacy_reader.py       #   V1 json → V2 domain object
│
├── providers/                 # Phase 2
├── accounts/                  # Phase 2
├── routing/                   # Phase 2
├── versioning/                # Phase 3
├── capabilities/              # Phase 4
├── conversation/              # Phase 5
├── brain/                     # Phase 6
├── scheduling/                # Phase 7
└── secrets/                   # Phase 3 (ต้องมาก่อน account จริง)
```

**import-linter contract ใหม่ที่ต้องเพิ่มพร้อม Phase 1:**

| contract | กติกา |
|---|---|
| `core-is-bottom-layer` | `agent_takkub.core.*` ห้าม import PyQt6, `orchestrator`, `main_window`, UI ใดๆ |
| `core-models-pure` | `core.models.*` ห้าม import อะไรนอกจาก stdlib + `core.models` ด้วยกัน |
| `core-contracts-pure` | `core.contracts.*` import ได้แค่ stdlib + `core.models` |

---

## 2. แผนราย Phase (ปรับจาก blueprint ให้เข้ากับความเป็นจริง)

| Phase | ชื่อ | ส่งมอบอะไร | ปล่อย release ได้ไหม | ประเมิน |
|---|---|---|---|---|
| **0** | Audit + baseline | เอกสาร 3 ไฟล์นี้ + test baseline | — | ✅ **เสร็จแล้ว** |
| **1** | Domain models + contracts | `core/models/`, `core/contracts/`, import contracts ใหม่, unit tests | ได้ (dead code ที่ไม่มีใครเรียก) | S |
| **2** | Provider adapter + Account + Router | wrap spawn 2 branch เป็น adapter, `ProviderAccount` registry, selector (priority/sticky/quota-aware), migrate `user_profile` | ได้ (flag ปิด = เดิม) | **L** |
| **3** | Secret + Version + Compatibility + Migration engine | `SecretManager`, `version.json`, compat matrix, migration dry-run/journal/rollback, `doctor` เพิ่มบรรทัด | ได้ | M |
| **4** | Capability Hub | รวม skill/mcp/plugin registry, ย้าย skill store ออกจากชื่อ `.claude` (คิวที่ user สั่งไว้แล้ว), permission engine 2 ชั้น | ได้ | M |
| **5** | Conversation V2 + Checkpoint | Takkub-owned message store, ingest adapter ต่อ provider, rolling summary, checkpoint, provider-switch | ได้ | **L** |
| **6** | Second Brain | Memory Manager + candidate pipeline + retrieval + Context Builder ที่ assignment-time + reflection ที่ done | ได้ | **L** |
| **7** | Scheduler + Resource | ขยาย `ResourceGovernor` เป็น provider/account/project slot + priority + backpressure + pause/checkpoint | ได้ | M |
| **8** | Storage V2 migration | แยก config/state/runtime/cache, `takkub migrate {inspect,plan,dry-run,apply,validate,rollback}` | ได้ (ต้องมี rollback) | **L** |
| **9** | UI/CLI | หน้า Accounts/Pools/Models/Routing/Brain/Scheduler/Migration ใน Settings | ได้ | M |
| **10** | Legacy deprecation | V2.0 legacy supported → V2.1 default V2 → V2.2 warning → V3.0 ลบ | ได้ | S |

**ข้อเสนอลำดับที่ต่างจาก blueprint:** blueprint วาง Version/Migration เป็น Phase 3 และ Secret ไว้ท้ายๆ
แผนนี้ **ดึง Secret Manager ขึ้นมาอยู่ Phase 3 ด้วย** เพราะ Phase 2 จะสร้าง `ProviderAccount` ที่ต้องอ้าง credential — ถ้าไม่มี `secretRef` ตั้งแต่ต้น จะได้ account registry ที่เก็บ path credential ดิบๆ แล้วต้องมา migrate ตัวเองอีกรอบ

**คิวงานของ user ที่ค้างอยู่และควรกลืนเข้ามาในแผนนี้:**
- ย้ายคลัง skill ออกจากชื่อ `.claude/` → **Phase 4** (user อนุมัติแล้ว รอทำ)
- ลื้อโครงสร้าง `~/.agent-takkub` → **Phase 8** (user บอกว่ามีแผนของตัวเองแล้ว — ต้องเอาแผนนั้นมาเป็น input ไม่ใช่เขียนใหม่)

---

## 3. Phase 1 — แผนละเอียด (พร้อมลงมือทันทีที่อนุมัติ)

### 3.1 เป้าหมาย
สร้าง **vocabulary กลาง** ของ V2 เป็น pure Python ที่ไม่มีใครเรียกใช้ ยังไม่แตะ production path แม้แต่บรรทัดเดียว
จบ phase นี้แล้วต้องพิสูจน์ได้ว่า: import contracts ใหม่บังคับใช้จริง + suite เดิมไม่ขยับแม้แต่ test เดียว

### 3.2 ไฟล์ที่จะ **สร้างใหม่**

```text
src/agent_takkub/core/__init__.py
src/agent_takkub/core/models/__init__.py
src/agent_takkub/core/models/provider.py
src/agent_takkub/core/models/account.py
src/agent_takkub/core/models/model.py
src/agent_takkub/core/models/agent.py
src/agent_takkub/core/models/task.py
src/agent_takkub/core/models/conversation.py
src/agent_takkub/core/models/capability.py
src/agent_takkub/core/models/memory.py
src/agent_takkub/core/models/version.py
src/agent_takkub/core/contracts/__init__.py
src/agent_takkub/core/contracts/provider_adapter.py
src/agent_takkub/core/contracts/account_selector.py
src/agent_takkub/core/contracts/secret_manager.py
src/agent_takkub/core/contracts/store.py
src/agent_takkub/core/contracts/migration.py
src/agent_takkub/core/storage/__init__.py
src/agent_takkub/core/storage/paths.py
src/agent_takkub/core/storage/jsonl_store.py

tests/test_core_models.py
tests/test_core_contracts.py
tests/test_core_jsonl_store.py
```

### 3.3 ไฟล์ที่จะ **แก้** (เท่านี้ — ไม่มีอย่างอื่น)

| ไฟล์ | แก้อะไร |
|---|---|
| `pyproject.toml` | เพิ่ม 3 import-linter contracts (`core-is-bottom-layer`, `core-models-pure`, `core-contracts-pure`) |
| `docs/architecture/depgraph.json` | regenerate ผ่าน `tools/gen_import_graph.py` (pre-commit บังคับอยู่แล้ว) |
| `docs/architecture/godfile-map.md` | เพิ่มบรรทัดชี้ไปที่ `docs/v2/` |

**ไฟล์ที่ Phase 1 จะ *ไม่* แตะเด็ดขาด:** `orchestrator.py`, `spawn_engine.py`, `cli.py`, `config.py`, `provider_spec.py`, UI ทั้งหมด

### 3.4 ข้อกำหนดการออกแบบ Phase 1

- ทุก model เป็น `@dataclass(frozen=True, slots=True)` — immutable โดยดีฟอลต์ เหมือน `ProviderSpec`/`DigestFacts` ที่มีอยู่
- ทุก contract เป็น `typing.Protocol` **แบบ sync** ไม่ใช่ `async def` (R1) — งาน I/O หนักยังใช้ QThread/`ProviderUsageStore` pattern เดิม
- MemoryRecord.trust (ยังไม่มี — Phase 1) ใช้ enum 5 ระดับที่ขยายจาก `digest_facts` : `cockpit_measured` / `user_confirmed` / `lead_confirmed` / `agent_reported` / `external_untrusted`
- ทุก persistent model มี `user_id` / `workspace_id` / `project_id` เป็น field ตั้งแต่ต้น แต่ implement เป็น single-user (R10)
- core/storage/paths.py (ยังไม่มี — Phase 1) **อ่านจาก `config.DATA_HOME` / `config.RUNTIME_DIR` ที่มีอยู่** ห้ามประกาศ home ใหม่ (กฎ blueprint ข้อ 6 ของ Brain pack เดิม)
- ยังไม่มี `ContinuationRecord` แยก — ใช้ `Checkpoint` + `Conversation` ตาม blueprint นี้ แต่ **ห้ามใช้คำว่า `HandoffRecord`** เพราะชนกับ `_task_handoff_pointer` ที่มีอยู่จริง

### 3.5 Definition of Done ของ Phase 1

- [ ] `pytest` เขียวทั้งหมด และ **จำนวน test เดิม 7,033 ไม่มีตัวไหนถูกแก้ expected value**
- [ ] `ruff check src/ tests/` ผ่าน
- [ ] `lint-imports` = 28 contracts kept, 0 broken
- [ ] `python -c "import agent_takkub.core"` ไม่ดึง PyQt6 เข้ามา (พิสูจน์ด้วย test ที่เช็ค `sys.modules`)
- [ ] cockpit เปิดได้ปกติ (smoke: spawn Lead + assign 1 งาน + done)
- [ ] CI เขียวทั้ง windows + macOS

---

## 4. Test Strategy

### 4.1 ระดับชั้น

| ชั้น | ครอบคลุมอะไร | รันเมื่อไหร่ |
|---|---|---|
| **Unit** (`tests/test_core_*.py`) | models, contracts, store, selector, retrieval — pure Python ไม่มี Qt | ทุก commit (targeted) |
| **Adapter contract** | ทุก `ProviderAdapter` ต้องผ่าน test suite ชุดเดียวกัน (parametrized ข้าม 6 provider) | ทุก commit |
| **Wiring/integration** | assign→delivery→done ผ่าน orchestrator จริง (flag เปิด/ปิด) | ทุก phase |
| **Migration** | dry-run + apply + validate + rollback บน fixture ของ V1 จริง | Phase 8 |
| **Live-store probe** | อ่าน store จริงของ provider บนเครื่อง (skip ถ้าไม่มี) | ก่อน release ทุกครั้ง — **บทเรียน D3** |
| **Full suite** | ทั้งหมด | ครั้งเดียวที่ qa batch gate ก่อน merge |

### 4.2 กฎที่มาจากบทเรียนจริงของโปรเจกต์

1. **ห้ามพิสูจน์ adapter ด้วย mock อย่างเดียว** — codex 0.147 ผ่าน CI เขียวตลอดเพราะ `codex exec` เขียน schema เก่า ในขณะที่ `codex-tui` (ตัวที่ pane ใช้จริง) เขียนแบบใหม่ → adapter ทุกตัวต้องมี test ที่กิน record จริงจาก store จริงอย่างน้อย 1 เคส
2. **parse ทั้ง schema เก่าและใหม่** — และมี test ยืนยันว่าไฟล์เดียวไม่ผสมสอง schema (กัน double-count)
3. **full suite เท่านั้นที่จับ fake signature drift ได้** — fake ที่ signature เพี้ยนจะ raise ใน QTimer slot → PyQt6 abort เงียบ exit 127 ซึ่ง targeted run ไม่เห็น
4. **feature-flag test คู่** — ทุก integration ต้องมีทั้งเคส flag เปิดและปิด และเคสปิดต้องได้ผลลัพธ์ byte-identical กับก่อน V2

---

## 5. Migration Strategy

### 5.1 หลักการ (ห้ามละเมิด)

```text
Detect V1 → Inventory → Backup → Parse semantics → Build V2 in memory
    → Validate → Dry-run report → Write V2 → Cross-check → Mark migrated
```

- **copy-never-move** จนกว่า validate ผ่าน — precedent: `provider_bootstrap.ensure_provider_home()` (1.0.74) ใช้ `.partial` + marker + `os.replace`
- **ไม่ลบ source** ในเวอร์ชันเดียวกับที่ migrate · ลบได้เร็วที่สุดคือ V3.0 ตาม deprecation ladder
- **unknown field ห้ามทิ้งเงียบ** — เก็บลง migration report
- **credential ห้าม mutate โดยไม่ backup**
- ทุก step **idempotent** + มี pre-check/post-check + journal

### 5.2 CLI ที่จะเพิ่ม (Phase 3 โครง, Phase 8 ของจริง)

```bash
takkub migrate inspect     # V1 อะไรอยู่ตรงไหน, schema version เท่าไหร่
takkub migrate plan        # จะย้ายอะไรไปไหน (ไม่แตะดิสก์)
takkub migrate dry-run     # ทำจริงลง temp + รายงาน diff
takkub migrate apply       # ทำจริง + journal
takkub migrate validate    # cross-check V2 กับ V1 ที่ยังอยู่
takkub migrate rollback    # ย้อนจาก journal + backup
```

### 5.3 ลำดับ migrate ที่ปลอดภัยที่สุด

จาก risk ต่ำ → สูง (แต่ละขั้นปล่อยแยก release ได้):

1. **read-only registries** — `provider-models.json`, `role-models.json`, `disabled-providers.json`, `exec-mode.json`, `rtk-enabled.json` (พังแล้วแค่ค่า default กลับมา)
2. **role/agent config** — `custom-roles.json`, `role-providers.json` → AgentTemplate + routing policy
3. **capability** — `pane-tools.json`, `skill-policy.json` + ย้าย skill store
4. **project data** — `projects.json` → `projects/<id>/project.json` + worktree ownership
5. **state** — issues, autoresume, remote sessions
6. **credential/account** — `claude-config/` → `providers/` + `accounts/` + `secrets/` ← **เสี่ยงสูงสุด ทำท้ายสุด**

**เกณฑ์หยุด:** ถ้าขั้นไหน validate ไม่ผ่านบนเครื่องของ user จริง → rollback แล้วหยุดทั้งขบวน ไม่ทำขั้นถัดไป

### 5.4 ความเข้ากันได้ระหว่างทาง

- ทุกไฟล์ V1 มี `LegacyReader` ที่แปลงเป็น V2 domain object **ตอนอ่าน** ตั้งแต่ Phase 1 — ทำให้ Core V2 ทำงานได้ก่อนที่ดิสก์จะย้ายจริง
- ดิสก์ย้ายจริงที่ Phase 8 เท่านั้น
- เครื่องที่ยังไม่ migrate ต้องเปิด cockpit ได้ปกติเสมอ (ทั้ง Windows และ macOS — ทดสอบทั้งคู่)

---

## 6. สิ่งที่ต้องตัดสินใจก่อนเริ่ม Phase 1

| # | คำถาม | ทางเลือก | ข้อเสนอ |
|---|---|---|---|
| Q1 | ทำ V2 เต็ม 10 phase หรือเลือกเฉพาะที่แก้ปัญหาจริงตอนนี้? | (a) เต็มตามแผน (b) เอาเฉพาะ 2/5/6 (account+conversation+brain) ซึ่งเป็นช่องว่างจริง | **(b) ก่อน** แล้วค่อยขยาย — 4 phase ที่เหลือแก้ปัญหาที่ยังไม่เจ็บ |
| Q2 | ทำ V2 บน branch ยาว หรือ merge เข้า main ทีละ phase? | (a) long-lived branch (b) main + feature flag | **(b)** — โปรเจกต์นี้ปล่อย release ถี่ branch ยาวจะ conflict กับ bug fix ปกติ (R9) |
| Q3 | แผนลื้อ `~/.agent-takkub` ของ user | user บอกว่าเขียนไว้แล้ว | ขอแผนนั้นมาเป็น input ของ Phase 8 — ห้ามเขียนทับ |
| Q4 | Tool Gateway | blueprint ต้องการ intercept ทุก tool call | ประกาศเป็น *Capability Gateway* ตาม R2 — ต้องให้ user รับทราบว่าข้อ 9 ของ HARD RULES ทำได้แค่บางส่วนกับ CLI provider |

---

## 7. Required Output ตาม blueprint §"REQUIRED OUTPUT AFTER AUDIT"

| ข้อ | อยู่ที่ไหน |
|---|---|
| 1. Current architecture map | `CURRENT_ARCHITECTURE_AUDIT.md` §1–2 |
| 2. Major technical debt | `CURRENT_ARCHITECTURE_AUDIT.md` §5 (D1–D10) |
| 3. Components safe to reuse | `REUSE_VS_REWRITE_MATRIX.md` (REUSE/WRAP) |
| 4. Components to replace | `REUSE_VS_REWRITE_MATRIX.md` (REPLACE/NEW) |
| 5. Risk list | `CURRENT_ARCHITECTURE_AUDIT.md` §7 (R1–R10) |
| 6. Proposed V2 module tree | เอกสารนี้ §1 |
| 7. Phase 1 implementation plan | เอกสารนี้ §3 |
| 8. Files Phase 1 will touch | เอกสารนี้ §3.2–3.3 |
| 9. Test strategy | เอกสารนี้ §4 |
| 10. Migration strategy | เอกสารนี้ §5 |
