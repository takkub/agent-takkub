# Core V2 — Phase 7a+7b: Second Brain (Memory Manager + Retrieval) (epic #309)

> worktree `wt/backend-2-1787128199`, base `feat/v2-core` (Phase 1–5 merged) · 2026-08-19

## สรุป

ทำครบ **7a** (candidate pipeline: `MemoryManager` + 4 source adapters) และ
**7b** (`RetrievalEngine` + `NativeBrainAdapter` + flag-gated façade) ใน
`src/agent_takkub/core/brain/`. ทุกอย่างใหม่ (**NEW**, ตรงกับ
`REUSE_VS_REWRITE_MATRIX.md` §4 "Memory Manager / candidate pipeline — ไม่มี
ของเดิม") ยกเว้น source adapters ที่ **WRAP** reader เดิม (`role_memory.py`,
`vault_mirror.py`'s knowledge base + decision notes, `digest_facts.py`) โดย
ไม่แก้ตัวอ่าน/เขียนเดิมเลยสักบรรทัด (ตรวจแล้วด้วย `git diff` — 4 ไฟล์นั้น
ไม่อยู่ใน diff) ส่วน `bm25_search.tokenize` ถูก **REUSE** ตรงตามที่ matrix
ระบุไว้แล้ว. ยังไม่เชื่อม orchestrator (7c Context Builder ทำ) —
`core.brain.facade` มีไว้เป็นจุดเข้าเดียวที่พร้อมให้ 7c เรียก.

## 7a — MemoryManager candidate pipeline

- **`src/agent_takkub/core/models/memory.py`** (EXTEND, backward-compat):
  - `Scope` เติม `USER`/`TASK`/`SESSION` (4 ค่าเดิม `GLOBAL`/`WORKSPACE`/
    `PROJECT`/`AGENT` ไม่แตะ)
  - `MemoryRecord` เติม field ใหม่ทั้งหมดต่อท้ายพร้อม default —
    `agent_id`/`task_id`/`session_id`/`source`/`version`/`status`
    (`RecordStatus` ใหม่: `ACTIVE`/`SUPERSEDED`)/`superseded_by`/
    `updated_at` — ยืนยันแล้วว่า `test_core_models.py` เดิม (frozen/slots,
    default `trust=AGENT_REPORTED`/`confidence=MEDIUM`/`scope=PROJECT`,
    Trust 5 ระดับ) ยังผ่านทุกตัวโดยไม่แก้ test นั้นเลย
  - `CandidateConfidence` ใหม่ (`CONFIRMED`=1.0/`HIGH`=.85/`INFERRED`=.6/
    `UNVERIFIED`=.4 ผ่าน `CONFIDENCE_WEIGHT`) — **แยกจาก** `Confidence`
    (LOW/MEDIUM/HIGH) เดิมโดยตั้งใจ เพราะ `Confidence` ถูก test เดิม pin
    ค่า default ไว้แล้ว การเปลี่ยน enum นั้นจะพัง `test_core_models.py`;
    `confidence_for()` map ลงจาก candidate scale ไปยัง record scale
    (CONFIRMED/HIGH→HIGH, INFERRED→MEDIUM, UNVERIFIED→LOW)
- **`src/agent_takkub/core/brain/candidate.py`**: `MemoryCandidate` — รูปแบบ
  เดียวที่เขียนเข้า brain ได้ (ไม่มี `id`/`version`/`status` เพราะ pipeline
  เป็นผู้กำหนดตอน persist)
- **`src/agent_takkub/core/brain/store.py`**: `BrainStore` — event-sourced
  บน `JsonlStore` เดิม (`src/agent_takkub/core/storage/jsonl_store.py` ไม่มี update-in-place
  primitive) หนึ่งไฟล์ต่อ project ใต้ `core_home()/brain/<project>.jsonl`
  (+ bucket `_global.jsonl` สำหรับ `project=None`) — "update" คือ append
  event ใหม่ id เดิม, `load_latest()` fold by id (last-write-wins จาก
  append order เดิม ไม่ต้องเทียบ version เอง), `load_active()` filter
  `status==ACTIVE`
- **`src/agent_takkub/core/brain/pipeline.py`**: `MemoryManager.submit_candidate`
  — **จุดเขียนเดียว** ของ Second Brain: validate (content ว่าง/สั้นเกินไป
  → `REJECTED`) → dedup (normalized text ตรงเป๊ะ = `DUPLICATE` no-op) →
  conflict detect ผ่าน token-set Jaccard similarity (reuse
  `bm25_search.tokenize`, threshold 0.6) ใน bucket เดียวกัน
  (kind+scope+project_id+agent_id — ไม่ scan ข้าม project) → ถ้า match แต่
  content ต่าง = conflicting update: record เก่า flip เป็น `SUPERSEDED` +
  `superseded_by=<new id>` (**ไม่ลบ**, ยัง query ได้ผ่าน `load_latest()`),
  record ใหม่ `version=old.version+1` → persist ทั้งคู่เป็น event ใหม่
- 4 source adapters ใน **`src/agent_takkub/core/brain/sources/`**:
  1. `role_memory_source.py` — parse bullet จาก `role_memory.py`'s
     learned-notes markdown (ข้าม seed placeholder), `trust=AGENT_REPORTED`,
     `scope=AGENT`
  2. `knowledge_source.py` — parse entry ``- `<iso>` **<role>** — <text>``
     จาก `vault_mirror.distill_to_knowledge_base`'s
     `runtime/knowledge/<project>.md`, `confidence=HIGH` (ผ่าน
     `_is_durable_fact` curation มาแล้วรอบหนึ่ง)
  3. `decision_note_source.py` — parse frontmatter + `## Note` body จาก
     `vault_mirror._render_decision_note`'s output (session decision notes)
  4. `digest_facts_source.py` — **ไม่อ่านไฟล์**: `DigestFacts` เป็น
     in-memory object (`digest_facts.py`'s docstring — build ครั้งเดียวต่อ
     `done()`, ไม่ persist เอง) รับ object ตรงๆ, `trust=COCKPIT_MEASURED`,
     `confidence=CONFIRMED`, `scope=TASK` ถ้ามี `ref` ไม่งั้น `PROJECT`

## 7b — RetrievalEngine + adapter + façade

- **`src/agent_takkub/core/brain/retrieval.py`**: `RetrievalEngine.recall(query,
  scope, budget_tokens)` — ranking = **bm25 + scope match + recency +
  confidence + importance** (ห้าม vector-only, ไม่มี embedding ที่ไหนเลย)
  - bm25: reuse `bm25_search.tokenize` (ASCII word + Thai trigram) แต่
    เขียน BM25 sum เอง เพราะ `bm25_search._bm25_rank` เป็น private +
    ผูกกับ `(meta, text)` จาก session/archive file, ไม่ใช่ `MemoryRecord`
    list — สูตรเดียวกัน (Okapi BM25 k1=1.5, b=0.75), normalize ด้วย max
    ในชุดผลลัพธ์
  - scope match: 1.0 ตรง scope, 0.5 ถ้า record เป็น `GLOBAL`, 0 อื่นๆ
  - recency: exponential decay ครึ่งชีวิต 30 วัน
  - confidence/importance: map จาก `Confidence`/`Trust` เป็น weight คงที่
  - `budget_tokens`: ตัดผลลัพธ์ที่ rank แล้วให้พอดี budget (ประมาณ
    4 chars/token — `ponytail:` comment ระบุ ceiling + upgrade path ไว้ใน
    docstring) แต่ **เก็บ top hit เสมอ** แม้เกิน budget คนเดียว กัน
    query ที่มีผลจริงกลับมาเป็น `[]`
- **`src/agent_takkub/core/brain/adapter.py`**: `NativeBrainAdapter` —
  implement `core.contracts.brain_adapter.BrainAdapter` (ตรวจแล้วผ่าน
  `isinstance()` กับ Protocol runtime_checkable) — `remember(record)`
  **ไม่เขียน record ตรง**: แปลงเป็น `MemoryCandidate` แล้วเรียก
  `MemoryManager.submit_candidate` เท่านั้น (ตรงกับ "agent ห้ามเขียน brain
  ตรง (API มีแต่ submit_candidate)")
- **`src/agent_takkub/core/brain/facade.py`** + **`flag.py`**:
  `TAKKUB_V2_BRAIN` off by default (เหมือน Phase 3's `TAKKUB_V2_ROUTER`
  pattern เป๊ะ) — flag off: `recall()`→`[]`, `submit()`→`None`, **ไม่สร้าง
  `BrainStore` เลย** (ยืนยันด้วย test ที่ monkeypatch `BrainStore` เป็น
  `AssertionError` แล้วเช็คว่าไม่ถูกเรียก) flag on: exception ใดๆ ใน path
  fail-open กลับเป็น `[]`/`None` เหมือนกัน ไม่ raise ออกไปหา caller

## Multi-provider / cross-platform

- ไม่มี path/command เฉพาะ platform ใดๆ ใน `src/agent_takkub/core/brain/*` — ใช้
  `pathlib.Path` ผ่าน `core.storage.paths.core_home()` เดิมทั้งหมด, ไม่มี
  `sys.platform` branch
- ไม่มีจุดใดผูก provider เฉพาะ (claude/codex/gemini/...) — candidate/record
  เป็น pure data, source adapters อ่าน markdown/object ที่ provider-neutral
  อยู่แล้ว (`role_memory.py`/`vault_mirror.py`/`digest_facts.py` ทุกตัวเขียน
  ไว้ในเอกสารตัวเองว่า provider-neutral by construction #103) — ไม่มี gap
  ใหม่ที่ต้อง flag เข้า #103 จาก phase นี้

## ไฟล์ที่สร้าง/แก้

**สร้างใหม่** (10 module + 5 test file):
- `src/agent_takkub/core/brain/__init__.py`
- `src/agent_takkub/core/brain/flag.py`
- `src/agent_takkub/core/brain/candidate.py`
- `src/agent_takkub/core/brain/store.py`
- `src/agent_takkub/core/brain/pipeline.py`
- `src/agent_takkub/core/brain/retrieval.py`
- `src/agent_takkub/core/brain/adapter.py`
- `src/agent_takkub/core/brain/facade.py`
- `src/agent_takkub/core/brain/sources/__init__.py`
- `src/agent_takkub/core/brain/sources/role_memory_source.py`
- `src/agent_takkub/core/brain/sources/knowledge_source.py`
- `src/agent_takkub/core/brain/sources/decision_note_source.py`
- `src/agent_takkub/core/brain/sources/digest_facts_source.py`
- `tests/test_core_brain_models.py`
- `tests/test_core_brain_pipeline.py`
- `tests/test_core_brain_sources.py`
- `tests/test_core_brain_retrieval.py`
- `tests/test_core_brain_adapter.py`

**แก้ไข**:
- `src/agent_takkub/core/models/memory.py` (extend, backward-compat —
  ดูรายละเอียดใน §7a ด้านบน)

## Test count

- ไฟล์ใหม่ 5 ไฟล์: **44 tests** (models 6, pipeline 13, sources 8,
  retrieval 8, adapter 9)
- targeted battery รวม (ไฟล์ใหม่ + ไฟล์เดิมที่แตะ/เกี่ยวข้องโดยตรง —
  `test_core_models.py`, `test_core_contracts.py`, `test_core_jsonl_store.py`
  รวมถึง no-Qt subprocess probe, `test_core_routing.py` เป็น sibling-phase
  regression check): **117 passed, 0 failed**
- ไม่ได้รัน full suite ตามนโยบาย targeted-tests-only (qa batch gate จะรัน
  เต็มก่อน merge)

## Lint-imports

`lint-imports` (28 contracts): **28 kept, 0 broken** — ยืนยันว่า
`core.brain.*` ไม่ import PyQt6/orchestrator/main_window/app/cli/
cli_server/agent_pane/terminal_widget (ตรง `core-is-bottom-layer` ซึ่ง
ครอบคลุม `agent_takkub.core` ทั้งต้นไม้อยู่แล้ว ไม่ต้องเพิ่ม contract ใหม่)

## Ruff

`ruff check` บน `src/agent_takkub/core/brain/` + `src/agent_takkub/core/models/memory.py`
+ 5 test file ใหม่: **All checks passed**

## Gap / #103 (ประกาศชัด ไม่เงียบ)

1. **ยังไม่เชื่อม orchestrator** — ตามขอบเขต task ("7c Context Builder จะ
   สั่งแยก") `core.brain.facade` เป็น entry point ที่พร้อมให้ 7c เรียก แต่
   ไม่มี call site จริงใน `done()`/spawn path วันนี้
2. **`RetrievalEngine.recall`'s `budget_tokens` เป็นการประมาณ 4 chars/token**
   ไม่ใช่ tokenizer จริง — ระบุ ceiling ไว้เป็น `ponytail:` comment ใน
   `retrieval.py`, upgrade path = สลับเป็น token counter จริงถ้า 7c
   ต้องการ hard guarantee
3. **`BrainStore.append()` เป็น O(n) rewrite ต่อ event** (สืบทอดมาจาก
   `JsonlStore.append`'s ponytail เดิมใน Phase 1 — ไม่ใช่ regression ใหม่)
   — เหมาะกับ corpus ต่อ-project ขนาดเล็กของ Phase 7 เท่านั้น ถ้า Second
   Brain โตจนมี record ต่อ project หลักหมื่น ต้องอัพเกรดเป็น true-append
   ก่อน (ระบุ upgrade path ไว้แล้วใน `jsonl_store.py`'s docstring เดิม)
4. **Conflict/dedup semantic key เป็น token-set Jaccard** (threshold 0.6)
   ไม่ใช่ embedding — ตรงกับ "ห้าม vector-only" ของ blueprint ตามที่ตั้งใจ
   แต่หมายความว่าการเปลี่ยนคำพูดที่ token overlap ต่ำ (เช่น synonym ล้วน)
   จะไม่ dedup กัน — ยอมรับ trade-off นี้ตามข้อจำกัด "ห้าม vector-only"
   ของ blueprint เอง ไม่ใช่ oversight
