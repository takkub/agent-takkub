# #160 — shard fan-out เขียนไฟล์รายงานทับกัน

**สาขา:** `wt/backend-1-1786593920` · **สถานะ:** fix ลงแล้ว, targeted tests ผ่านหมด

## ปัญหา

`qa --plan --shards N` (และ plain `--shards N` แบบไม่ plan) ส่ง **task text เดียวกันเป๊ะ** ให้ทุก
shard (`qa#1..qa#N`) — ถ้า task นั้นสั่งให้เขียนรายงาน/ผลลัพธ์ไปที่ path คงที่ (เช่น
`docs/audit/xxx.md`) ทุก shard ก็เขียนไปที่ path เดียวกันพร้อมกัน ตัวที่เขียนทีหลังทับตัวที่เขียนก่อน
— เสียรายงานของ shard อื่นไปเงียบๆ โดยไม่มี error ใดๆ ส่งกลับ

Root cause: cockpit มี `TAKKUB_SHARD` / `TAKKUB_SHARD_TOTAL` env vars อยู่แล้ว (identity ของ pane)
แต่**ไม่เคยใช้แยกความแตกต่างใน task text ที่ role เห็น** — ปล่อยให้ role ทุกตัวตัดสินใจเอง (ไม่มี
mechanism บังคับ) ว่าจะเขียนไฟล์ output ที่ path ไหน

## Fix

จุดเดียวที่ทุก assign (ทั้ง plain fan-out จาก `cli.py` และ plan-mode fan-out จาก
`pipeline_executor.py::_fire_qa_plan_fanout`) ไหลผ่านคือ `orchestrator.py::_assign_dispatch` —
ก่อน spawn จริง มี `if plan and shard_total > 0:` (ห่อ task ให้ planner pane) แล้วต่อด้วย
`elif shard_total > 0:` ใหม่ (ห่อ task ให้ shard pane จริง):

```python
elif shard_total > 0:
    shard_idx = _split_shard(role_name)[1] or 0
    delivery_task = self._wrap_shard_task(task, shard_idx, shard_total)
```

`PipelineMixin._wrap_shard_task` (`pipeline_executor.py`) ต่อท้าย task เดิมด้วย block ที่บอก shard
index/total ชัดเจน + สั่งว่า **ถ้า task บอกให้เขียนไฟล์ path คงที่ ห้ามเขียนทับตรงๆ ให้เติม
suffix `.shard{n}` ก่อนนามสกุลไฟล์เสมอ** (เช่น `report.md` → `report.shard2.md`) แล้วรายงาน path
จริงกลับใน `takkub done`

จุดเดียวนี้ครอบทั้งสองเส้นทาง fan-out เพราะทั้งคู่ปิดท้ายด้วย `self.assign(...) → _assign_dispatch`
เหมือนกัน — ไม่ต้องแก้ `cli.py` หรือ `_fire_qa_plan_fanout` แยก

### Safety net (ข้อ 4 ในแผน — เผื่อ role เมินคำสั่ง)

คำสั่งใน task text เป็นแค่ prose ที่ agent อาจไม่ทำตาม — เพิ่มการตรวจจับหลัง fact ด้วย:
`PipelineMixin._detect_shard_path_collisions(done: dict)` สแกน done-note ของทุก shard หา token
ที่หน้าตาเหมือน file path (regex heuristic, นามสกุล md/json/txt/csv/html/log/yaml) ถ้า path
เดียวกันถูกพูดถึงโดย ≥2 shard → `_inject_shard_fanout_handoff` (consolidated handoff ตอนทุก shard
done) จะแปะ warning `⚠️ [#160 guard] หลาย shard พูดถึงไฟล์ path เดียวกัน…` พร้อม path + shard index
ที่ชนกัน ให้ Lead เห็นก่อนเชื่อว่ารายงานครบ

### ที่ไม่ทำ (จากแผน 4 ข้อ)

ข้อ 3 ("เตือน Lead ตอน assign ถ้าหลาย shard ชี้ output path เดียวกัน") ไม่ทำเป็น pre-assign heuristic
แยก — เพราะ plain fan-out ทุก shard ได้ task text เหมือนกันเป๊ะอยู่แล้ว (ตรวจจับ "จะชนไหม" จาก raw
text ก่อน assign แม่นยำน้อยกว่าและซ้ำซ้อนกับ fix เชิงโครงสร้างข้างบน) — safety net หลัง fact (ตรวจจาก
done-note จริง) ให้สัญญาณที่แม่นกว่าและ actionable กว่า

## Tests

`tests/test_orchestrator_shard.py` — เพิ่ม 10 tests ใหม่ (รวม 51 → 61 ในไฟล์นี้):
- `TestWrapShardTask` — shape ของ `_wrap_shard_task` (index/total, suffix ต่างกันตาม shard)
- `TestAssignInjectsShardNoteIntoTaskText` — `orch.assign(shard_total>0, plan=False)` จริง ต้องห่อ
  task ด้วย SHARD note (และ non-shard assign ต้องไม่ถูกห่อ)
- `TestDetectShardPathCollisions` — unit test ตรง ๆ ของ regex/collision detector
- `TestShardFanoutHandoffWarnsOnCollision` — integration: 2 shard done-note พูดถึง path เดียวกัน →
  consolidated handoff มี `#160 guard` warning; suffix ต่างกัน → ไม่มี warning

```
tests/test_orchestrator_shard.py   61 passed
tests/test_qa_plan_fanout.py       14 passed
tests/test_pipeline_executor.py    37 passed  (unaffected — regression check)
= 112 passed
```

ruff check + ruff format ผ่านทั้ง `pipeline_executor.py`, `orchestrator.py`,
`tests/test_orchestrator_shard.py`

## ไฟล์ที่แตะ

- `src/agent_takkub/pipeline_executor.py` — `_wrap_shard_task` (ใหม่),
  `_detect_shard_path_collisions` (ใหม่), `_inject_shard_fanout_handoff` (เพิ่ม collision check),
  `_SHARD_PATH_RE` (constant ใหม่)
- `src/agent_takkub/orchestrator.py` — `_assign_dispatch` เพิ่ม `elif shard_total > 0:` branch
- `tests/test_orchestrator_shard.py` — 12 tests ใหม่
