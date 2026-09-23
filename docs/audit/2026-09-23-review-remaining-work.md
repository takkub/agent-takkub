# System review — งานที่เหลือ (ส่งต่อให้รันผ่าน cockpit)

> เขียนไว้ 2026-09-23 · เอกสารนี้คือ **runbook สำหรับ Lead ใน cockpit** ให้สั่งงานต่อได้เองด้วย `takkub assign`
> โดยไม่ต้องใช้ ultracode workflow อีก · ข้อมูลทุกอย่างที่ต้องใช้ถูกย้ายเข้า repo แล้ว (`docs/audit/data/`)
> จึงไม่ขึ้นกับ session ไหนทั้งสิ้น

## สถานะถึงตอนนี้

| ชุด | งาน | สถานะ |
|---|---|---|
| 1 | อ่านโค้ดทุก subsystem (170k LOC) หาบั๊กใช้งานจริง | ✅ 335 ข้อ — `2026-09-22-system-review-batch1-findings.md` |
| 2 | ค้านบั๊ก severity high 39 ข้อ (tracer + skeptic) | ✅ 38 ยืนยัน / 1 refuted — `2026-09-23-system-review-batch2-verified.md` |
| 6 | แก้ 38 ข้อที่ยืนยัน + เทสกันถอย | ✅ PR #713 (v2.1.33) |
| **3-5** | **dead code / dead tests → ลบ** | ⬜ **ยังไม่ทำ — runbook อยู่ข้างล่าง** |
| **med** | **ค้าน + แก้ 127 ข้อ severity med** | ⬜ **ยังไม่ทำ** |
| low | 169 ข้อ (83 ข้อในนั้น category = dead → รวมกับชุด 3-5) | ⬜ ทำท้ายสุด/ข้ามได้ |

## ไฟล์ข้อมูล (อยู่ใน repo แล้ว)

| ไฟล์ | เนื้อหา |
|---|---|
| `docs/audit/data/2026-09-22-review-findings-335.json` | บั๊กดิบทั้ง 335 ข้อ (file, line, severity, category, scenario, evidence, fix, confidence, group) |
| `docs/audit/data/2026-09-23-review-verified-high-39.json` | 39 ข้อ high + ผลค้านของ tracer/skeptic (confirmed 38 / refuted 1) |
| `docs/audit/data/2026-09-22-vulture-min60.txt` | 411 candidate จาก `vulture --min-confidence 60` |
| `docs/audit/data/2026-09-22-zero-importer-modules.json` | 50 โมดูลที่ไม่มีใคร import (จาก depgraph) |

สร้าง vulture ใหม่ได้ด้วย: `uvx vulture src/agent_takkub --min-confidence 60 --exclude "*/static/*,*/_assets/*"`

---

## ชุด 3 — dead code (โมดูลที่ไม่มีใคร import)

**เป้า:** 50 โมดูลใน `2026-09-22-zero-importer-modules.json` · ตัดสินทีละตัวว่า **ตายจริง** หรือ **ยังถูกเรียกแบบที่ depgraph มองไม่เห็น**

⚠️ **ห้ามเชื่อ depgraph อย่างเดียว** — repo นี้มี hidden edge เยอะ (อ่าน `docs/architecture/godfile-map.md` ตาราง "Hidden edges"):
`importlib`/string dispatch · `python -m` entrypoint ใน `pyproject.toml`/`package.json`/`npm/`/`scripts/`/`.github/workflows` ·
ตาราง `cmd` ใน `cli_server.py` · string ใน `hook_wiring.py` · registry ที่ key ด้วยชื่อ · Qt signal/slot name ·
JS ใน `remote/static/` เรียก HTTP route · เทสที่ import (= โมดูลตายในโปรดักต์ แต่เทสก็ต้องลบด้วย)

```bash
takkub assign --role backend --scope deep --task-file docs/audit/tasks/batch3-dead-modules.md
```

เนื้อ task (เขียนใส่ไฟล์แล้วค่อย assign — กัน backtick เพี้ยน ตาม `docs/lead/cli-reference.md`):

> อ่าน `docs/audit/data/2026-09-22-zero-importer-modules.json` (50 โมดูล ไม่มีใคร import ตาม depgraph)
> สำหรับแต่ละโมดูล: (1) อ่าน header ว่าเดิมทำอะไร (2) `git log -3 --oneline -- <path>` ดูบริบท
> (3) Grep ทั้ง repo (src, tests, docs, scripts, npm, .claude, .github, pyproject.toml, package.json, remote/static)
> หา basename + ชื่อ public ของมัน รวม dynamic form ทุกแบบข้างบน
> ตัดสิน: `dead` (ไม่เจอ reference ในโปรดักต์เลย + ไม่มี entrypoint) / `alive` / `uncertain`
> ผลลง `docs/audit/2026-09-23-dead-modules-verdict.md` เป็นตาราง: module · verdict · หลักฐาน · ถ้าลบต้องลบอะไรตามด้วย (เทสที่มีไว้เพื่อมันอย่างเดียว)
> **ห้ามลบอะไรในรอบนี้** — รอบนี้ตัดสินอย่างเดียว

## ชุด 4 — dead code (ฟังก์ชัน/เมธอด/ตัวแปรที่ไม่ถูกใช้)

**เป้า:** 411 candidate ใน `2026-09-22-vulture-min60.txt`

⚠️ **ตัวที่ vulture มักเข้าใจผิด (ต้องตอบ alive):** PyQt override ที่ Qt เรียกเอง (`paintEvent`/`closeEvent`/`eventFilter`/`sizeHint`) ·
pytest fixture · dataclass field ที่อ่านผ่าน `asdict`/JSON · `__all__` export · re-export facade ใน `orchestrator.py` ·
ชื่อที่ถูกเรียกผ่าน `getattr(self, "name")` หรือ dict dispatch · constant ที่ export ไว้เพื่อ backward compat

```bash
# แบ่ง 411 บรรทัดเป็น 4 ชิ้น (ชิ้นละ ~103) ทำทีละชิ้นหรือใช้ --shards 4
takkub assign --role backend --shards 4 --scope deep --task-file docs/audit/tasks/batch4-vulture.md
```

> อ่าน `docs/audit/data/2026-09-22-vulture-min60.txt` เอาเฉพาะช่วงบรรทัดของ shard ตัวเอง (shard N จาก 4)
> แต่ละ candidate: เปิดที่นิยาม → Grep ทั้ง repo หาชื่อนั้น (รวม string form + รายการยกเว้นข้างบน) → ตัดสิน dead/alive/uncertain
> ตัวแปรที่ assign แล้วไม่เคยอ่าน = dead เว้นแต่เป็น dataclass field / config key / constant ที่ export ไว้
> ผลลง `docs/audit/2026-09-23-dead-symbols-verdict-shard<N>.md` ตาราง: path:line · ชื่อ · kind · verdict · หลักฐาน

## ชุด 5 — dead tests

**เป้า:** เทส 574 ไฟล์ (`tests/test_*.py`)

**นับว่าเทสตาย:** skip/xfail ถาวรที่ไม่มีทางรัน · เทสเฉพาะโค้ดที่ชุด 3-4 ตัดสินว่า dead · เทสฟีเจอร์ที่ถูกถอดไปแล้ว
(patch target ชี้ attribute ที่ไม่มีอยู่จริง จึงผ่านเพราะไม่เคยถูกเรียก) · เทสที่ล้มไม่ได้ (assert True, assert ว่า mock คืน mock) ·
เทสซ้ำบรรทัดต่อบรรทัดในไฟล์เดียวกัน · fixture/helper ที่ไม่มีใครใช้

⚠️ **ห้ามนับว่าตาย:** guard test ที่ตั้งใจ assert เนื้อหา docs/role file (`test_agent_role_files_have_*`, `test_lead_docs_guard`,
`test_*_guard.py`) — พวกนี้คือด่านบังคับนโยบาย · เทสที่ skip ด้วยเงื่อนไข runtime จริง (ไม่มี binary/คนละ OS)

```bash
takkub assign --role qa --shards 4 --scope deep --task-file docs/audit/tasks/batch5-dead-tests.md
```

> Glob `tests/test_*.py` เรียงตามชื่อ แบ่ง 4 ส่วน เอาส่วนของ shard ตัวเอง อ่านทุกไฟล์จนจบ
> เกณฑ์ตายและข้อยกเว้นตามด้านบน · ผลลง `docs/audit/2026-09-23-dead-tests-verdict-shard<N>.md`

## ชุด 5b — prosecutor (ด่านสุดท้ายก่อนลบ) — **ห้ามข้าม**

ทุกอย่างที่ชุด 3-5 ตัดสินว่า `dead` ต้องผ่านคนค้านอีกชั้นก่อนลบจริง

```bash
takkub assign --role reviewer --mode code --scope deep --task-file docs/audit/tasks/batch5b-prosecute.md
```

> คุณคือ**ฝ่ายค้านการลบ** อ่าน verdict ทุกไฟล์จากชุด 3-5 (`docs/audit/2026-09-23-dead-*-verdict*.md`)
> ทุกรายการที่ถูกตัดสินว่า dead: ไล่หา live reference ที่คนก่อนหน้าอาจพลาด (grep ทั้ง repo + ทุก dynamic form)
> เทส: ดูว่ามันกันพฤติกรรมจริงอยู่ไหม (รันไฟล์นั้นไฟล์เดียวถ้าจำเป็น)
> ตอบต่อรายการ: เจอ reference ที่ไหนไหม · ลบได้ปลอดภัยไหม
> **เข้มไว้ก่อน: "ลบผิด" แย่กว่า "เก็บไว้เกิน"** · ผลลง `docs/audit/2026-09-23-deletion-approved.md`

## ชุด 5c — ลบจริง

```bash
# แตก branch ใหม่จาก main ก่อนเสมอ
git checkout main && git pull && git checkout -b review/dead-code-removal
takkub assign --role backend --scope deep "ลบเฉพาะรายการที่ docs/audit/2026-09-23-deletion-approved.md อนุมัติ (safe_to_delete เท่านั้น) · ลบทั้งสาย: โค้ด + import ที่อ้างถึง + เทสที่มีไว้เพื่อมันอย่างเดียว + บรรทัดใน docs ที่อ้างถึง (กฎ 'เอา UI ออก = ถอน dead code ทั้งสาย') · ห้ามลบอะไรที่ไม่อยู่ในรายการอนุมัติ · หลังลบ: ruff check/format, lint-imports, python tools/gen_import_graph.py, รัน tests/test_*guard*.py ทั้ง glob + test_issues.py + test_core_contracts.py + ไฟล์เทสที่เกี่ยวข้อง · รายงานว่าลบอะไรไปกี่บรรทัด"
```

---

## med 127 ข้อ

`2026-09-22-review-findings-335.json` → กรอง `severity == "med"` · **ยังไม่ผ่านการค้าน** (ชุด 1 เป็นความเห็นของผู้อ่านคนเดียว)

แนะนำทำเป็นรอบๆ ตามพื้นที่ (คอลัมน์ `group`) แทนที่จะทำทีเดียว 127 ข้อ:

```bash
# ตัวอย่าง: กลุ่ม lead-inbox
takkub assign --role backend --scope deep "อ่าน docs/audit/data/2026-09-22-review-findings-335.json เอาเฉพาะ severity=med และ group='lead-inbox' · แต่ละข้อ: ไล่ path จริงจาก entry point (takkub CLI → cli_server → orchestrator / watchdog tick / UI click / hook) ว่าเกิดกับผู้ใช้จริงได้ไหม แล้วพยายามหักล้างด้วย (มี guard/try-except/invariant/เทสคุมอยู่แล้วไหม · ถูกแก้ไปแล้วใน 2.1.32/2.1.33 หรือยัง) · ข้อที่ยืนยันว่าจริง → แก้ + เทสกันถอย · ข้อที่หักล้างได้ → บันทึกเหตุผล · รายงานลง docs/audit/2026-09-23-med-<group>.md"
```

กลุ่มที่มี med เยอะสุด (จากชุด 1): `knowledge` 20 · `core-brain` 17 · `lead-inbox` 16 · `provider-helpers` 15 · `quota` 15 ·
`settings-window` 15 · `settings-mgmt` 14 · `editor-services` 14 · `cli-server-guard` 14 · `pipeline-tasks` 14 · `orchestrator-B` 14

---

## กฎที่ต้องบังคับทุกชุด (บทเรียนจากรอบนี้)

1. **ทีมแก้ห้ามแตะไฟล์เดียวกัน** — แบ่งงานตามไฟล์ ไม่ใช่ตามหัวข้อบั๊ก ไม่งั้น edit ชนกัน (รอบนี้แบ่ง 15 กลุ่มตามไฟล์ ไม่มีปัญหาเลย)
2. **`pane_guard.py` / `cli.py` / `config.py` / `orchestrator.py` / `spawn_engine.py` = โค้ดที่ hook ของทุก pane ใช้อยู่**
   แก้แล้วต้อง `python -c "import agent_takkub.<module>"` ทันที syntax error = ทุก pane รัน Bash ไม่ได้ทั้งเครื่อง
3. **รัน `tests/test_*guard*.py` ทั้ง glob ก่อน push เสมอ** (ไม่ใช่เลือกบางไฟล์) — รอบก่อน CI แดงเพราะ
   `test_subprocess_text_encoding_guard.py` จับ `encoding="utf-8-sig"` ที่เพิ่งเพิ่ม
4. **แตะ writer ที่ใช้ `cached_read` → ต้องเรียก `invalidate()` เสมอ** — คลาสบั๊กที่เจอซ้ำรอบที่ 3 แล้ว (#685-#687 → รอบนี้อีก 6 จุด)
5. **worker QThread ห้าม parent กับ dialog/window ที่ปิดได้** (#688) — Qt abort ฆ่าทั้งโปรแกรม จับด้วย faulthandler ไม่ได้
6. **ห้ามให้ pane เขียนวลี quota banner ลง output** — detector ของ cockpit อ่านจอ pane แล้วเข้าใจผิดว่าชนโควตา (#704)
7. **ลบอะไรก็ตามต้องผ่าน prosecutor ก่อน** และแตก branch แยก + PR ทุกครั้ง
