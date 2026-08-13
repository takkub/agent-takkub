# Integration รอบ 2 รวม 7 branch — 2026-08-13

## สรุป

รวมทั้งหมด **7 branch** เข้า branch เดียว: **`wt/devops-1786617396`** (ใช้ pane worktree branch เดิมเป็น integration branch) แตกจาก **`release/2026-08-13`** (remote-only ตอนเริ่มงาน — ต้อง `git fetch` ก่อน) ที่ commit `09eb489` ซึ่งผ่าน CI 10/10 มาแล้วบน PR #184

branch เริ่มต้นของ pane นี้ (`df1f65f`) เป็น ancestor ของ `origin/release/2026-08-13` อยู่แล้ว (0 commit ต่าง, 26 commit ตาม) → fast-forward ตรงไปที่ release base ก่อนเริ่มรวม (ไม่ใช่ merge, เป็น ff-only ที่ปลอดภัย)

ทุก branch merge สำเร็จด้วย `git merge --no-ff` **ไม่มี conflict เกิดขึ้นเลยสักตัว** (0/7) — grep `^<<<<<<<|^=======$|^>>>>>>>` ทั่ว `src/ tests/ docs/` หลังรวมครบว่างเปล่า จุดที่คาดว่าจะชน (`orchestrator.py`, `limit_panel.py`/`usage_meter.py`, `depgraph.json`) ทั้งหมด auto-merge ได้เองด้วย `ort` strategy เพราะ diff อยู่คนละ hunk/บรรทัดกัน

## ลำดับการรวม + commit

| กลุ่ม | Branch | Merge commit | ไฟล์ที่คาดว่าชน | ผลจริง |
|---|---|---|---|---|
| A.1 | `wt/frontend-1786615682` — ลบตัวเลข usage ปลอมออกจาก limit_panel/usage_meter (BLOCKER) + 6 เทสกันของปลอมกลับมา | `540de0e` | `limit_panel.py`, `usage_meter.py` | clean, ไม่มี conflict |
| A.2 | `wt/backend-1786615563` — #169 kill guard (pane_guard.py + role file 16 ไฟล์) + #179 race ใน pty_session.py | `d2f4975` | — (ตั้งต้นจาก `df1f65f` ไม่ใช่ release base ตรงๆ ตามที่แจ้งไว้) | clean, merge ปกติ ไม่มี conflict |
| A.3 | `wt/backend-2-1786615563` — #174 issues.py อ่าน local backlog + GitHub รวมกัน | `b5bea73` | — | clean |
| A.4 | `wt/backend-1786616583` — #168 knowledge base (vault_mirror.py +148, orchestrator.py hook) | `a2db2bd` | `orchestrator.py` | auto-merged (+11 บรรทัดเข้า orchestrator.py คนละ hunk กับของเดิม) |
| A.5 | `wt/qa-1786616082` — #178 playwright --output-dir + #182 md5 dedup + #167 test lock + #177 mitigation (stagger 3s) | `a6eb3c9` | `orchestrator.py`, `depgraph.json`, `cli_server.py` | auto-merged ทุกไฟล์ (คนละ hunk) |
| B.1 | `wt/devops-1786615562` — wave A audit (#170/#171/#172/#176 = duplicate ของ #139/#140/#141/#142) | `3dc30a8` | — | clean (ไฟล์ใหม่, docs เท่านั้น) |
| B.2 | `wt/devops-1786616413` — wave E audit (#183/#180 = duplicate ของ #160/#161) | `df88dd7` | — | clean (ไฟล์ใหม่, docs เท่านั้น) |

**ไม่มีจุดไหนต้องตัดสินใจทิ้งฝั่งใดฝั่งหนึ่ง** — ทุก feature จากทั้ง 7 branch อยู่ครบใน merge tree

## depgraph.json

`docs/architecture/depgraph.json` auto-merge (ort) ผ่านได้เองในทุก merge ที่แตะไฟล์นี้ (A.5) ตามกฎ "auto-gen conflict → regenerate ไม่แก้มือ" ได้รัน `tools/gen_import_graph.py` ซ้ำหลังรวมครบทั้ง 7 branch เพื่อยืนยันความสด (module_count=141)

**หมายเหตุ pitfall ที่เจอระหว่างทาง:** worktree นี้มี `.venv` แยกของตัวเอง (สร้างใหม่ระหว่างงานนี้ ติด `grimp==3.15`) แต่ pre-commit hook `depgraph-fresh` resolve `.venv` จาก **git-common-dir ของ main worktree เสมอ** (`$ROOT/.venv/Scripts/python.exe`) ซึ่ง main worktree ติดตั้ง `grimp==3.14` — regenerate ด้วย venv คนละตัวได้ค่า `"generated_by"` ต่างกัน (`3.15` vs `3.14`) ทำให้ commit แรกๆ ติด hook fail วนไปมา (`git add` แล้ว rtk wrapper รายงานสถานะไม่ตรง จนต้องเช็คด้วย `git rev-parse HEAD` / `git show --stat HEAD` แทน `git log --oneline` ที่ให้ output ผิดพลาดตอนนั้น) แก้โดย **regenerate ด้วย main-worktree venv ตัวเดียวกับที่ hook ใช้จริง** (`ROOT="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"`) แล้วพบว่าไฟล์ที่ auto-merge มาจาก branch qa (`8dd886d`) มีค่า `grimp 3.14` อยู่แล้วตรงกับ main venv พอดี → **ไม่ต้อง commit เพิ่ม** ยืนยันด้วยการรัน hook command ตรงๆ (`git diff --exit-code docs/architecture/depgraph.json` หลัง regen) → exit 0, ไฟล์สดอยู่แล้วจริง

## Targeted tests (รันหลังรวมแต่ละ branch)

| Branch | ไฟล์เทสที่รัน | ผล |
|---|---|---|
| `wt/frontend-1786615682` | `test_limit_panel_teardown.py`, `test_usage_meter_spend.py` | ✅ 9 passed |
| `wt/backend-1786615563` | `test_cli_guard.py`, `test_pane_guard.py`, `test_pty_session_reader_proc_race.py`, `test_agent_role_files_have_host_destructive_guard.py` | ✅ passed |
| `wt/backend-2-1786615563` | `test_issues.py`, `test_cli_server.py`, `test_orchestrator_stall.py` | ✅ passed |
| `wt/backend-1786616583` | `test_knowledge_base.py` | ✅ 22 passed |
| `wt/qa-1786616082` | `test_browser_mcps.py`, `test_cli_server.py`, `test_done_evidence.py`, `test_issue_167_adhoc_instance_isolation.py`, `test_qa_plan_fanout.py` | ✅ passed |
| `wt/devops-1786615562`, `wt/devops-1786616413` | ไม่มี — docs-only, ไม่มีไฟล์เทส | — |

## Full suite (รันครั้งเดียวตอนจบตามที่สั่ง — จะ push ให้ CI ตรวจ)

```
.venv/Scripts/python.exe -m pytest -q
```

**✅ 5709 passed, 7 skipped, 0 failed, 0 errors** (exit code 0) — skip 7 ตัวเป็น Qt-dependent test ปกติที่ skip อยู่แล้วในทุกรอบ ไม่ใช่ของใหม่จากการรวมรอบนี้

## Lint / architecture gates

- `.venv/Scripts/lint-imports.exe` → **23/23 contracts kept, 0 broken**
- `.venv/Scripts/python.exe -m ruff check src/ tests/` → **All checks passed**

## ตรวจ fake-usage fix (blocker check ตามที่สั่งเน้น)

```
grep -rn "_fake_other_provider_usages" src/
```
→ **ไม่พบเลย** (grep exit 1) ทั้งก่อน merge branch อื่นๆ (เช็คทันทีหลัง merge frontend) และหลังรวมครบทั้ง 7 branch — ไม่มี branch ไหนดึงของปลอมกลับมา ยืนยันว่า `_fake_other_provider_usages` ถูกลบออกจาก `limit_panel.py` แล้วจริง เหลือแค่ regression-guard test (`test_no_fake_other_provider_usages_symbol_left_in_module`) ที่อ้างชื่อฟังก์ชันไว้เพื่อกันของปลอมกลับมาในอนาคต

## ข้อควรระวัง / สิ่งที่ Lead ควรรู้ก่อนไปต่อ

- `wt/backend-1786615563` ตั้งต้นจาก `df1f65f` (commit เก่าก่อน fast-forward ไป release base) ไม่ใช่ release base ตรงๆ ตามที่ Lead แจ้งไว้ล่วงหน้า — merge ผ่านปกติไม่มีปัญหา เพราะ `df1f65f` เป็น ancestor ของทั้ง release base และ HEAD ปัจจุบันอยู่แล้ว (3-way merge ธรรมดา ไม่ต้อง rebase)
- ยังไม่ได้ push — รอ Lead review diff + ตัดสินใจ merge เข้า `release/2026-08-13` หรือ `main`
- venv ของ worktree นี้สร้างใหม่ (`.venv` เดิมไม่มี) แล้ว editable install `pip install -e ".[dev]"` — ไม่ได้แตะ venv ของ worktree อื่น

## Branch สุดท้ายที่มีของครบ

**`wt/devops-1786617396`**
Latest commit: **`df88dd7`** (Merge wt/devops-1786616413 — merge สุดท้ายในลำดับ 7 branch, มีของครบทุกอย่าง, `depgraph.json` สดอยู่แล้วไม่ต้อง commit เพิ่ม)

ลำดับ merge commit เต็ม (เก่า→ใหม่): `7b60f63` (release base, มาก่อนแล้ว) → `540de0e` → `d2f4975` → `b5bea73` → `a2db2bd` → `a6eb3c9` → `3dc30a8` → `df88dd7`
