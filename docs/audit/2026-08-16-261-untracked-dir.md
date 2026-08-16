# #261 — untracked directory ยุบชื่อทำให้ไฟล์ลึกหลุดจากรายงาน

## ปัญหา (จาก #251)

`snapshot_porcelain_paths()` เก็บ fingerprint `(XY, mtime_ns, size)` เฉพาะ path ที่
`git status --porcelain -z` (ไม่มี `-uall`) คืนมา — สำหรับ untracked directory git
จะยุบเหลือชื่อโฟลเดอร์ชั้นนอกสุดเท่านั้น เช่น `?? new_folder/`

ถ้า `new_folder/` มีอยู่ก่อน assign แล้ว agent สร้าง/แก้ไฟล์ลึกเข้าไป
(`new_folder/deep/file.py`) โดยที่ `new_folder/deep` มีอยู่ก่อนแล้ว — mtime/size ของ
`new_folder/` (โฟลเดอร์แม่) จะไม่เปลี่ยนเลย เพราะ inode/entry ของโฟลเดอร์แม่เอง
ไม่ถูกแตะ (การเขียนไฟล์ลูกใน dir ลูกไม่กระทบ stat ของ dir ปู่) → `changed_dirty_paths()`
เทียบ baseline vs current แล้วเท่ากัน → คัดทิ้ง = **false negative** ไฟล์ที่แตะจริงหายจากรายงาน

## ทางที่เลือก

เลือกตัวเลือกที่ 2 ตามที่ระบุใน issue: **ถ้า entry เป็น directory (path ลงท้าย `/`)
ให้ถือว่า "เปลี่ยนเสมอ"** แทนการเทียบ metadata — ไม่เลือก `--untracked-files=all` เพราะ
ต้องวัด cost เพิ่มบน repo ที่มี `node_modules`/dependency ใหญ่ (I/O เดิน tree ลึกทุก query)
และมีความเสี่ยงเห็น noise จากไฟล์ที่ไม่เกี่ยวข้องในโฟลเดอร์เดียวกัน — การถือว่า
"directory entry เปลี่ยนเสมอ" ถูกกว่า ไม่ต้อง measure เพิ่ม และปลอดภัยฝั่ง false
positive (โฟลเดอร์โผล่ในรายงานทั้งที่ไม่มีอะไรใหม่ข้างในจริง) ซึ่งดีกว่า false negative
ตามหลักที่ user วางไว้ว่า "ข้อมูลผิดที่ดูน่าเชื่อถือ แย่กว่าไม่มีข้อมูล"

## การแก้ไข

`changed_dirty_paths()` ใน `src/agent_takkub/worktree_manager.py`: เดินลูปบน union ของ
key ทั้งสอง snapshot เหมือนเดิม แต่ path ที่ลงท้ายด้วย `/` (directory entry จาก porcelain)
จะถูกนับเป็น "เปลี่ยน" ทันทีที่ปรากฏในฝั่งใดฝั่งหนึ่ง โดยไม่เทียบ metadata เลย — ส่วน
path ปกติ (ไฟล์) ยังคงเทียบ `(status, mtime_ns, size)` equality เหมือนเดิม ไม่กระทบ
พฤติกรรมเดิมของไฟล์ที่ git status รายงานตรงๆ (tracked file, top-level untracked file)

ไม่แตะ `snapshot_porcelain_paths()` เอง — ยังคง lstat แค่ path ที่ porcelain คืนมา
(ยุบเป็นชื่อโฟลเดอร์) เหมือนเดิม การแก้จบที่ชั้นเปรียบเทียบ (`changed_dirty_paths`)
เพียงจุดเดียว

## เทส

`tests/test_worktree_manager.py` เพิ่ม 2 เคสใน `TestDirtyPathSnapshots`:

1. `test_untracked_directory_entry_always_reported_even_with_unchanged_metadata` —
   unit เดิม baseline/current metadata เท่ากันเป๊ะสำหรับ `new_folder/` แต่ต้อง
   ยังโผล่ในผลลัพธ์
2. `test_untracked_directory_deep_edit_survives_real_snapshot_roundtrip` — repro
   เต็มรูปแบบตาม issue: สร้าง `new_folder/deep/file.py` จริงบน filesystem ก่อน
   snapshot แรก (baseline), แก้เนื้อไฟล์ (เปลี่ยนทั้ง mtime และ size ของไฟล์ลูก),
   snapshot อีกครั้งด้วย porcelain string เดิม (`?? new_folder/\n` — จำลองพฤติกรรม
   ยุบของ git status จริง) แล้วเรียก `changed_dirty_paths()` — ต้องได้
   `["new_folder/"]` กลับมา ไม่ใช่ `[]`

รันเฉพาะ targeted: `pytest tests/test_worktree_manager.py -k DirtyPathSnapshots`
(5 passed) และทั้งไฟล์ `tests/test_worktree_manager.py` (117 passed) ยังเขียวทั้งหมด
— ไม่ได้รัน full suite ตามนโยบาย targeted-tests-mid-flight

## Follow-up: Lead review เจอ false positive กลับมา (#261 fix-loop รอบ 2)

### ปัญหาที่ Lead ชี้

ทางแก้รอบแรก ("path ลงท้าย `/` = เปลี่ยนเสมอ") กัน false negative ได้จริง แต่**เอา
false positive กลับมาในรูปแบบเดิมที่ #251 ตั้งใจกำจัด**: ถ้า user มี untracked
directory ค้างอยู่ในทรีอยู่แล้วก่อน assign (เช่น `screenshots/`, `tmp/`, `.scratch/`)
โฟลเดอร์นั้นจะโผล่เป็น "ไฟล์ที่แตะ" ใน**ทุก** pane **ทุก**รอบ แม้ไม่มีใครแตะเลย — นี่คือ
เคสจริงที่ #251 ยกมา (screenshot ค้างในทรีโผล่ในทุก report) เพียงแค่ย้ายจากระดับไฟล์
มาเป็นระดับโฟลเดอร์เท่านั้น ไม่ได้แก้ปัญหาต้นตอ

### ทางแก้รอบ 2

เมื่อ porcelain entry เป็น directory (ลงท้าย `/`) ให้ **ขยายเฉพาะโฟลเดอร์นั้น** ด้วย
`git status --porcelain -z -uall -- <dir>` (scope ผ่าน pathspec แคบแค่ path เดียว
ไม่ใช่ `-uall` ทั้งทรี จึงไม่แพงแบบที่รอบแรกกังวล) แล้วเก็บ snapshot เป็น fingerprint
ระดับไฟล์ของไฟล์ข้างในแทน entry โฟลเดอร์เดี่ยว — ทำให้กลับไปเทียบ metadata แบบ
ไฟล์ต่อไฟล์ได้เหมือนเดิม (ไม่ใช่ "เปลี่ยนเสมอ" อีกต่อไป) โดยไม่เจอ false negative
ของ #261 เพราะไฟล์ลูกที่ถูกแก้จะมี mtime/size ของ**ตัวมันเอง**เปลี่ยนตรงๆ (ไม่ต้อง
พึ่ง stat ของโฟลเดอร์แม่แบบทางแก้เดิมของ #261 ที่ผิด)

โครงสร้างโค้ด (`src/agent_takkub/worktree_manager.py`):

- `snapshot_porcelain_paths(git_root, porcelain, runner=None, *, dir_expand_cap=2000)`
  — เพิ่ม parameter `runner` (optional, default `None`) ยังคง pure/ไม่พัง unit test
  เดิมที่เรียกแบบไม่ส่ง runner (เคส "ไม่มี git process ให้เรียก" — fallback เป็น
  พฤติกรรมเดิมของรอบแรก คือถือว่าเปลี่ยนเสมอ)
- `_expand_dir_entry(runner, git_root, dir_path, cap)` — รัน `-uall` scoped กับ dir
  เดียว, คืน `None` เมื่อ git fail หรือ entries เกิน cap (สัญญาณ fallback)
- `_DIR_EXPAND_CAP = 2000` — กันเคส untracked dir ใหญ่ผิดปกติ (เช่น `node_modules/`
  ที่หลุด .gitignore) ไม่ให้เดิน+stat เป็นพันไฟล์ทุก assign/done — เกิน cap แล้ว
  fallback เป็น "โฟลเดอร์เดียวเปลี่ยนเสมอ" (ปลอดภัยฝั่ง false positive เหมือนรอบแรก)
- `WorktreeManager.dirty_snapshot(git_root, porcelain)` — wrapper เรียก
  `snapshot_porcelain_paths(..., self._run)` ให้ assign-time
  (`shared_tree_baseline`) กับ done-time (orchestrator digest) ใช้ runner เดียวกัน
  แบบสมมาตร — ทั้งสองจุดเรียกผ่าน method นี้เท่านั้น (`orchestrator.py` เปลี่ยนจาก
  import free function ตรงๆ มาเรียก `mgr.dirty_snapshot(...)` แทน)

`changed_dirty_paths()` ไม่ต้องแก้เพิ่ม — กติกา "trailing `/` = เปลี่ยนเสมอ" ยังอยู่
และทำหน้าที่เป็น fallback path เท่านั้น (เมื่อ expand ไม่สำเร็จ/เกิน cap/ไม่มี runner)

### เทสเพิ่ม (`TestDirtyDirExpansion`, `tests/test_worktree_manager.py`)

1. `test_untouched_preexisting_untracked_dir_not_reported` — dir มีอยู่ก่อน assign,
   ไม่มีใครแตะ → ต้อง **ไม่** โผล่ในผลลัพธ์ (เคส false positive ที่ Lead เจอ)
2. `test_new_file_deep_inside_preexisting_untracked_dir_is_reported` — dir มีอยู่ก่อน
   assign, มีไฟล์ใหม่เพิ่มลึกเข้าไประหว่างงาน → ต้องโผล่ (กัน regression ของ #261)
3. `test_expansion_failure_falls_back_to_folder_marker` — runner คืน error → fallback
   เป็น folder marker เดิม (เปลี่ยนเสมอ)
4. `test_expansion_over_cap_falls_back_to_folder_marker` — entries เกิน
   `dir_expand_cap` → fallback เหมือนกัน
5. `test_worktree_manager_dirty_snapshot_uses_its_own_runner` — ยืนยันว่า
   `WorktreeManager.dirty_snapshot()` ส่ง `self._run` เข้าไปจริง (args มี `-uall`,
   `--`, path ของ dir)

`test_untracked_directory_deep_edit_survives_real_snapshot_roundtrip` (เทสเดิมของ
#261 รอบแรก) แก้ docstring ให้ชัดว่าเทสนี้จงใจไม่ส่ง runner (จำลองเคส "ไม่มี git
process") จึงยัง assert fallback behavior เดิม — ไม่ใช่ regression

รันเฉพาะ targeted: `pytest tests/test_worktree_manager.py tests/test_done_digest_facts_wiring.py tests/test_done_evidence.py tests/test_lifecycle_recovery.py`
— ทั้งหมดเขียว (144 passed) ไม่ได้รัน full suite

`tests/test_done_digest_facts_wiring.py::TestSharedTreePaneDigestFacts::test_snapshot_present_computes_commits_and_files`
ต้องเพิ่ม `dirty_snapshot()` ให้ fake `_SharedFake` ด้วย (มัน mock `WorktreeManager`
ทั้งตัว ก่อนหน้านี้ไม่มี method นี้เพราะ orchestrator เพิ่งเปลี่ยนมาเรียกผ่าน
`mgr.dirty_snapshot()` ในรอบนี้) — fake ใหม่ delegate ไปที่
`wm_mod.snapshot_porcelain_paths(git_root, porcelain)` แบบไม่ส่ง runner (พฤติกรรม
เท่าของเดิมของเทสนี้ ซึ่งไม่มี directory entry ให้ขยายอยู่แล้ว)

## หมายเหตุ environment

worktree นี้ไม่มี `.venv` ของตัวเอง — ใช้ shared `.venv` ที่ repo หลัก
(`C:\Users\monch\WebstormProjects\agent-takkub\.venv`) ซึ่ง editable-install ชี้ไปที่
`src` ของ repo หลัก ไม่ใช่ worktree นี้ เพื่อไม่ไปรัน `pip install -e .` ทับ shared venv
(กฎ #202) จึงรันเทสด้วย `PYTHONPATH=src` ควบคู่ python ของ shared venv แทน — ยืนยันแล้ว
ว่า import resolve เข้า `worktree_manager.py` ของ worktree นี้จริง (เช็คด้วย
`m.__file__`) ก่อนรันเทส
