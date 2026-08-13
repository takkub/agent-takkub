# #161 — isolated worktree ที่ done โดยไม่มี commit เคยถูกลบเงียบๆ

**สาขา:** `wt/backend-1-1786596858` · **สถานะ:** fix ลงแล้ว, targeted tests ผ่านหมด

## ปัญหา

`Orchestrator._finalize_worktree` (เรียกจากทั้ง `done()` และ `close()`) ตัดสินจาก
`WorktreeManager.commit_count()` เพียงอย่างเดียว:

* `commits > 0` → ส่ง merge proposal ให้ Lead, เก็บ worktree ไว้ (ถูกอยู่แล้ว)
* `commits == 0` → เรียก `mgr.safe_remove(info)` **ทันที** — ถ้า tree สะอาด (ไม่ dirty)
  worktree + branch ถูกลบจริงโดยไม่มี Lead notice ใดๆ เลย มีแค่ `_log_event("worktree_removed", ...)`
  เงียบๆ ในไฟล์ log

เกิดขึ้นจริงกับ token-burn feature รอบแรก: pane รายงาน `takkub done` โดยไม่เคย `git commit`
(ทั้งที่ task สั่งชัดเจนว่าต้อง commit) → `_finalize_worktree` เห็น `commits == 0` + tree สะอาด (ไม่มี
อะไรค้างใน git status — ไม่ว่าเพราะ pane ไม่ได้แก้ไฟล์ที่ track จริง หรือ discard งานตัวเองไปก่อน) →
`safe_remove` ลบ worktree + branch ทิ้งทันที งานหายสนิท ไม่มี dangling commit ให้กู้เพราะไม่เคย commit
ตั้งแต่แรก และไม่มี notice ใดๆ ไปถึง Lead ให้ทันสังเกต

Case ที่ tree dirty (`commits == 0` แต่มี uncommitted changes) ถูก handle ถูกต้องอยู่แล้ว — `safe_remove`
ปฏิเสธลบ + `_finalize_worktree` แจ้ง Lead ("เก็บไว้ (ไม่ลบ)"). ช่องโหว่อยู่เฉพาะกรณี **clean + zero commits**
เท่านั้น ซึ่งเป็น branch เดียวที่ไม่มี Lead-facing signal ใดๆ เลย

## Fix (เลือกข้อ 1 + ข้อ 2 จาก 3 ข้อเสนอ)

`_finalize_worktree`: เมื่อ `commits == 0` (ไม่ว่า dirty หรือไม่) **ไม่เรียก `safe_remove` อีกต่อไป** —
ลบล้าง auto-delete ทิ้งไปเลย แล้วแจ้ง Lead แบบเดียวกันทั้งสอง sub-case (ต่างกันแค่ข้อความบอกสถานะ
dirty/clean):

```python
dirty = mgr.is_dirty(info)
_log_event("worktree_no_commit_kept", role=from_role, project=project_ns,
            branch=info.branch, dirty=dirty)
self._notify_lead(
    project_ns,
    f"⚠️ [{from_role}] done แต่ไม่มี commit ใน worktree `{info.branch}` — "
    f"เก็บไว้ไม่ลบอัตโนมัติ ({state_note}) · path: {info.path} · "
    f"ตรวจสอบแล้วค่อยลบเองด้วย `takkub worktree clean`",
    from_role=from_role, note="",
)
```

ผลคือ **ไม่มี isolated worktree ตัวไหนถูกลบอัตโนมัติจาก `done()`/`close()` อีกต่อไป** — ทุกเส้นทางจบที่
"เก็บไว้ + แจ้ง Lead" เท่านั้น (มี commit → merge proposal, ไม่มี commit → warning) ต่างจากเดิมที่มี
เส้นทางเงียบซ่อนอยู่หนึ่งเส้น

Cleanup ที่แท้จริง (ลบ worktree ที่ไม่มี commit) ยังทำได้ปกติผ่าน `takkub worktree clean` ที่มีอยู่แล้ว
(`WorktreeManager.clean_isolated`) — ใช้เกณฑ์ safe เดียวกัน (clean + no commits ahead) แต่เป็น
**Lead สั่งเองแบบ explicit** เท่านั้น ไม่ใช่ auto-fire ตอน pane done — pattern เดียวกับที่ #132 ใช้แยก
`orphan-worktrees` (auto-safe) ออกจาก `orphan-worktrees-review` (ต้อง `--level review --yes` ชัดเจน)

### ข้อ 3 (snapshot ก่อนลบ) — ไม่ทำ

เพราะ fix ข้างบนทำให้ไม่มีการลบอัตโนมัติเกิดขึ้นอีกแล้วในเส้นทางนี้ (ข้อ 2 ครอบคลุมโดยธรรมชาติ) —
snapshot ก่อนลบไม่จำเป็นอีกต่อไปสำหรับ done()/close() path เพราะไม่มีการลบเกิดขึ้นจนกว่า Lead จะสั่งเอง
ผ่าน `takkub worktree clean` (ซึ่งมี dry-run/print-target อยู่แล้วเป็น safety net ของมันเอง)

## ที่ไม่แตะ

* `WorktreeManager.safe_remove()` (`worktree_manager.py`) — ยังเก็บไว้ตามเดิม เป็น utility ที่ถูก unit
  test ตรงๆ อยู่แล้วใน `tests/test_worktree_manager.py` (refuse-if-dirty, unlink-links-before-remove,
  branch cleanup) แม้ตอนนี้ไม่มี caller ใน `src/` แล้วก็ตาม — ไม่ลบเพราะเป็น safety primitive ที่มีค่า
  อาจถูกเรียกใช้อีกในอนาคต (เช่น explicit `takkub worktree remove`) และการลบจะทำให้เสีย test coverage
  ของ git-safety logic ที่ทดสอบไว้ดีอยู่แล้ว — เกินขอบเขตของ #161 ที่โฟกัสที่ auto-delete call site
  ไม่ใช่ตัว primitive เอง
* `_prune_orphan_worktrees*` (`disk_usage.py`, #132) — คนละ trigger (boot-time sweep ของ worktree ที่
  cockpit จำไม่ได้แล้ว) ไม่เกี่ยวกับ live `done()`/`close()` path นี้

## Tests

`tests/test_worktree_assign.py` (`TestFinalizeWorktree`):
- `test_empty_clean_worktree_is_kept_and_warns` (เปลี่ยนชื่อ+เขียนใหม่จาก
  `test_empty_clean_worktree_is_safe_removed` เดิม) — ยืนยัน `safe_remove_calls == 0` + Lead ได้ warning
  ที่มี "เก็บไว้ไม่ลบอัตโนมัติ", "ไม่มี commit", "worktree clean"
- `test_dirty_worktree_kept_and_warns` — ปรับ assertion ให้ตรงกับข้อความใหม่ ยังคง `safe_remove_calls == 0`
- `test_commits_produce_merge_proposal`, `test_finalize_never_raises` — ไม่แตะ ยังผ่านเหมือนเดิม

```
tests/test_worktree_assign.py    12 passed
tests/test_worktree_manager.py   63 passed  (unaffected — regression check)
= 75 passed
```

`ruff check` + `ruff format --check` ผ่านทั้ง `orchestrator.py`, `tests/test_worktree_assign.py`

(หมายเหตุ: `tests/test_lead_context_compact.py::TestParallelModeWorktreeRule::test_solo_mode_has_no_parallel_block`
fail อยู่ก่อนแล้วบน main โดยไม่เกี่ยวกับ diff นี้เลย — verified ด้วย `git stash` แล้ว repro เหมือนเดิม
บน clean tree ก่อน commit นี้)

## ไฟล์ที่แตะ

- `src/agent_takkub/orchestrator.py` — `_finalize_worktree`: ลบ auto-`safe_remove` branch, รวม
  dirty/clean เป็น warn-and-keep เดียว, log event ใหม่ `worktree_no_commit_kept` (แทน
  `worktree_removed`/`worktree_kept` เดิม)
- `tests/test_worktree_assign.py` — อัปเดต 2 tests ให้ตรงพฤติกรรมใหม่ + docstring หัวไฟล์
