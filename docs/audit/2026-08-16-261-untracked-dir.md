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

## หมายเหตุ environment

worktree นี้ไม่มี `.venv` ของตัวเอง — ใช้ shared `.venv` ที่ repo หลัก
(`C:\Users\monch\WebstormProjects\agent-takkub\.venv`) ซึ่ง editable-install ชี้ไปที่
`src` ของ repo หลัก ไม่ใช่ worktree นี้ เพื่อไม่ไปรัน `pip install -e .` ทับ shared venv
(กฎ #202) จึงรันเทสด้วย `PYTHONPATH=src` ควบคู่ python ของ shared venv แทน — ยืนยันแล้ว
ว่า import resolve เข้า `worktree_manager.py` ของ worktree นี้จริง (เช็คด้วย
`m.__file__`) ก่อนรันเทส
