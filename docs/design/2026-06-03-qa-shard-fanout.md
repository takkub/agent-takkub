---
title: QA shard fan-out — parallel UI smoke testing
date: 2026-06-03
status: draft (pending gemini cross-check)
author: Lead
---

# QA shard fan-out — parallel UI smoke

## 1. Problem

QA smoke ทดสอบ UI **ทีละหน้า ใน pane เดียว Chrome เดียว** → ช้าเป็นเชิงเส้น (12 หน้า = 12× เวลา 1 หน้า). UI smoke เป็นงาน **embarrassingly parallel** — หน้า N ไม่ depend หน้า M.

## 2. Current constraints (verified in code)

| ข้อ | หลักฐาน | ผลต่อดีไซน์ |
|---|---|---|
| pane keyed by **role string** ต่อ project | `orchestrator.py:943` `_project_panes(project)[pane.role.name] = pane` | 1 role = 1 pane → spawn `qa` ซ้ำ = reuse pane เดิม ❌ |
| role validated via `validate_name` | `orchestrator.py:975` | key ใหม่ต้องผ่าน validation (กัน path traversal) |
| QA ใช้ `mb` CLI = **1 Chrome/pane** | `qa.md:55,137` | parallel ต้องแยก Chrome + แยก debug port |
| teammate pane **ห้าม spawn subagent** | `qa.md:5` | parallelism ต้องอยู่ระดับ **pane** ไม่ใช่ subagent |
| done event keyed `{ns}::{role}` | `orchestrator.py:1008` | shard ต้อง report done แยกกัน Lead aggregate |

**ข้อสรุป:** subagent-in-pane = ผิด (browser collision + invisible to cockpit). กลไกถูก = **หลาย QA pane (shard) แต่ละ pane Chrome ของตัวเอง**.

## 3. Proposed design

### 3.1 Pane instance id แยกจาก role

ตอนนี้ pane key = role name. เปลี่ยนเป็น **instance key** = `qa#1`, `qa#2`, … โดย:
- **role/behavior** ยังเป็น `qa` (อ่าน `.claude/agents/qa.md` เดิม)
- **pane key** = `qa#<n>` (n = shard index)

ทำให้ `_panes_by_project[ns]` เก็บ `{"qa#1": pane, "qa#2": pane, ...}` ได้หลายตัว

**กระทบจุดน้อยที่สุด** — เพิ่ม helper แยก `(role, shard)` จาก key:
```python
def _split_shard(key: str) -> tuple[str, int | None]:
    # "qa#2" → ("qa", 2);  "qa" → ("qa", None)
    if "#" in key:
        role, _, idx = key.partition("#")
        return role, int(idx)
    return key, None
```
ทุกที่ที่ map key → role file ใช้ `_split_shard(key)[0]`. `validate_name` ต้องอนุญาต `#` (หรือ validate role-part แยก แล้วต่อ `#n` ทีหลัง — ปลอดภัยกว่า)

### 3.2 CLI surface

```bash
takkub assign --role qa --shards 3 "<task spec>"
```
- `--shards N` → orchestrator spawn `qa#1..qa#N`, แต่ละ pane รับ task เดียวกัน + env `TAKKUB_SHARD=<n>` `TAKKUB_SHARD_TOTAL=N`
- **ไม่มี `--shards`** → behavior เดิม 100% (key = `qa`, ไม่มี suffix) — backward compatible

### 3.3 Chrome port isolation

`mb-start-chrome` ต้องรับ port ต่าง shard. inject env:
```
TAKKUB_SHARD=2  →  CHROME_DEBUG_PORT = 9222 + 2 = 9224
```
QA task spec (planner-generated) สั่ง `mb-start-chrome --port $CHROME_DEBUG_PORT` หรือ `mb` อ่าน env เอง (เช็คว่า mb รองรับ — ถ้าไม่ ต้อง patch wrapper)

### 3.4 Work split — ใครแบ่งหน้า?

**ทางเลือก A (แนะนำ): planner pane ก่อน fan-out**
```
qa (planner) → อ่าน app เขียน docs/qa-plan/<date>.md: ลิสต์หน้า + แบ่ง N ก้อน
  ↓ Lead อ่าน plan
qa#1 รับก้อน 1 · qa#2 รับก้อน 2 · qa#3 รับก้อน 3  (ขนาน)
```
**ทางเลือก B: shard แบ่งเอง** — แต่ละ shard อ่าน `TAKKUB_SHARD/_TOTAL` แล้ว self-select หน้า (เช่น hash route % total == shard). ไม่ต้อง planner แต่เสี่ยงแบ่งเหลื่อม/ตกหล่นถ้า route discovery ไม่ตรงกัน

### 3.5 Aggregate

- shard ทุกตัว `takkub done "shard 2/3: หน้า 5-8 ผ่าน · shots: ..."`
- orchestrator รู้ว่า assign นี้มี N shard → รอครบ N done → inject **1 handoff** เข้า Lead ("qa fan-out complete: 3/3 shards done")
- ต่อยอด `--auto-chain` ได้ (ทุก shard done → fire reviewer)

## 4. Token analysis

| | sequential 1 pane | shard × N |
|---|---|---|
| work tokens (click/assert) | X | **X** (เท่าเดิม) |
| image tokens (screenshots) | Y | **Y** (ช็อตเท่าเดิม) |
| context overhead (sys+role+mb) | 1× | **N×** ← เพิ่ม |
| context accumulation | 🔴 บวมท้าย O(pages²) | ✅ แต่ละ shard เล็ก |
| **wall-clock** | N× | **~1×** |

สุทธิ: token เพิ่ม ~`(N-1)×overhead` หักลบ accumulation saving → **เพิ่มเล็กน้อย ไม่ใช่ N เท่า**; เวลาลด ~N เท่า. คุ้ม.

## 5. Work items

1. **orchestrator**: `_split_shard` helper + pane registry รับ `qa#n` key + `validate_name` อนุญาต shard suffix
2. **CLI** (`cli.py`): `assign --shards N` → loop spawn qa#1..N + inject `TAKKUB_SHARD*` env
3. **mb / env**: `CHROME_DEBUG_PORT` ต่อ shard + เช็ค `mb-start-chrome --port`
4. **done aggregation**: count shards, รอครบ N → handoff เดียว (กระทบ `_auto_chain_panes` logic)
5. **qa.md**: เพิ่ม section "shard mode" — อ่าน env, port, แบ่งงาน
6. **tests**: `test_routing_planner` / orchestrator shard spawn + aggregate

## 6. Open questions (สำหรับ gemini)

- Q1: `qa#n` key หรือแยก field `shard` ใน AgentPane ดีกว่า? (key มี side-effect ทั่ว codebase ที่ assume key == role)
- Q2: planner-first (A) vs self-select (B) — robustness vs latency tradeoff?
- Q3: Chrome port collision เมื่อหลาย **project** shard พร้อมกัน — base port ต่อ project ด้วยไหม?
- Q4: shard ตัวนึ่ง crash — fail ทั้ง assign หรือ partial-pass + Lead เห็น gap?
- Q5: UI grid — N pane โผล่พร้อมกันใน cockpit เปลือง screen real estate; ควร collapse/group shard ไหม?
- Q6: มี simpler approach ที่ได้ parallel เหมือนกันแต่แตะ core น้อยกว่านี้ไหม?
