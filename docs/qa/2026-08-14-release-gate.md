# Release gate — 2026-08-14 (final, post 5-issue merge)

## บริบท
รอบนี้ merge #190 (proactive compact ยิงซ้ำ), #187 (worktree clean live-pane guard),
#186 (gemini/agy trust-prompt hang), #185 (dev-instance project label), #177 (Playwright
MCP บน qa shard, merged ก่อนหน้า) เข้า `main` — คนละ worktree มา ตรวจว่ารวมกันแล้วยังถูก

## 1. Full pytest suite — ✅ 0 failed
```
.venv/Scripts/python.exe -m pytest -q
```
- **5768 passed, 7 skipped, 0 failed** (exit 0)
- baseline รอบก่อน (2026-08-13): 5731 passed / 7 skipped → **+37 tests** (ตรงกับเทสใหม่ที่มากับ
  #186 `test_auto_trust_wait_window.py` + `test_delivery_blocked_prompt.py` + เพิ่มใน
  `test_pty_ready_prompt.py`/`test_launch_session.py`, #190 เพิ่มใน `test_idle_watchdog.py`,
  #187 เพิ่มใน `test_cli.py`/`test_worktree_assign.py`/`test_worktree_manager.py`)
- รันผ่าน `.venv` editable install (ไม่ใช้ system python + PYTHONPATH) ตาม project memory

## 2. Static checks — ✅ ทั้งหมดเขียว
| เช็ค | ผล |
|---|---|
| `ruff check src/ tests/` | All checks passed! |
| `ruff format --check src/ tests/` | 404 files already formatted |
| `lint-imports` | **24 kept, 0 broken** (Analyzed 140 files, 543 dependencies) — เพิ่มจาก 23 รอบก่อน แต่ broken = 0 |
| `python tools/gen_import_graph.py --check` | fresh, module_count=140, exit 0 |

## 3. pre-commit --all-files — ✅ ทั้ง 6 hooks Passed
```
Detect hardcoded secrets ............... Passed
ruff (legacy alias) ..................... Passed
ruff format .............................. Passed
takkub docs-verify ....................... Passed
import-linter architecture contracts .... Passed
depgraph freshness check ................. Passed
```
Exit 0 ทุก hook ไม่มี hook ไหน modify ไฟล์ทิ้งไว้

## 4. git status — ✅ สะอาด
```
git status
* main...origin/main [ahead 14]
clean — nothing to commit
```
รันหลังทุกขั้นตอนข้างบนแล้ว ไม่มี hook เขียนไฟล์ค้าง

## 5. ตรวจ integration หลัง merge (จุดสำคัญที่สุดของรอบนี้)

### 5a. `_check_proactive_compact` (#190) × folder-trust prompt detection (#186)
ทั้งคู่แตะ readiness signal ที่เกี่ยวข้องกัน — ตรวจแล้ว **ไม่ตีกัน**:

- `_check_proactive_compact()` (orchestrator.py:4006-4180) gate การไม่ยิง `/compact` ด้วย
  `not sess.is_at_ready_prompt() or sess.shows_startup_marker() or
  sess.is_blocked_on_tty_prompt() or ...` (บรรทัด 4071-4074) — **ไม่ได้เรียก
  `is_at_trust_prompt()` ตรงๆ**
- แต่ `is_at_ready_prompt()` เอง (pty_session.py:1076) มา classify ผ่าน
  `_classify_ready(_ready_region(...))` ซึ่งใช้ `READY_HARD_BLOCKERS` จาก
  `provider_spec.py:276-282` — ลิสต์นั้นมี `"trust this folder"` และ
  `"do you trust the contents of this directory"` เป็น **hard blocker ที่เช็คก่อน ready
  marker ใดๆ เสมอ** (`_classify_ready` comment บรรทัด 417: "checks hard blockers first,
  before any ready marker")
- ผลคือ: เมื่อ pane โชว์ trust-folder modal → `is_at_ready_prompt()` return `False` อยู่แล้ว
  โดยไม่ต้องพึ่ง `is_at_trust_prompt()`'s regex fix ของ #186 เลย → `_check_proactive_compact`
  เข้า not-ready branch ถูกต้อง ไม่มีทางยิง `/compact` ใส่ pane ที่ค้างอยู่ trust modal
- **#186's fix** (`_ENTER_CONFIRM_RE` แทน exact substring "enter to confirm") แก้เฉพาะ
  `is_at_trust_prompt()` ซึ่งใช้แยกต่างหากโดย `_auto_trust()` (auto-press Enter) และ
  `_send_when_ready` (blocked-prompt warning) — คนละ caller คนละหน้าที่กับ
  `_check_proactive_compact` ไม่มี overlap ที่จะพัง
- **PaneState field เพิ่มพร้อมกัน**: #190 เพิ่ม `proactive_compact_pending: bool` field,
  #186 ไม่แตะ `PaneState` dataclass เลย (แก้เฉพาะ `_auto_trust`/`_launch_session` signature +
  `lead_inbox.py`) → ไม่มี field-collision

**สรุป: ไม่พบจุดตีกัน** — ยืนยันด้วยการอ่าน source จริง (ไม่ใช่แค่ต่างคนต่างเขียว)

### 5b. Project label (#185) × worktree live-pane lookup (#187)
ทั้งคู่ "ดูเหมือน" พึ่ง project identity แต่จริงๆ **เป็นคนละ mechanism กัน ไม่ชนกัน**:

- **#185** แก้เฉพาะ `instance_identity_label()` (config.py:230-247) — ใช้แค่ตอนสร้าง
  **display string** `"dev · {name}"` ที่โชว์ใน `takkub list`/version banner เท่านั้น
  (`name = os.environ.get("TAKKUB_PROJECT") or REPO_ROOT.name`) เป็น cosmetic label ล้วนๆ
- **#187**'s live-pane guard ใช้คนละ path: `Orchestrator.live_worktree_paths(project)` →
  `self._resolve_project(project)` (orchestrator.py:925-933) ซึ่ง resolve จาก
  `active_project()` (registry state) ไม่ใช่จาก `TAKKUB_PROJECT` env หรือ `REPO_ROOT.name`
  เลย แล้ว scoped ผ่าน `_project_panes(project_ns)` ซึ่งเป็น dict key ที่ตั้งตอน spawn
  (`project_name` ที่ orchestrator เก็บใน `_panes_by_project`) — คนละแหล่งความจริงกับ label
- ยืนยันด้วย `git show b1e9ba1 -- config.py`: diff ทั้งหมดอยู่ใน `instance_identity_label()`
  ฟังก์ชันเดียว ไม่แตะ `_resolve_project`/`live_worktree_paths`/`_panes_by_project` เลย

**สรุป: ไม่พบจุดตีกัน** — #185 เป็น display-only change, #187's live-pane matching อาศัย
canonical project namespace (orchestrator registry) ที่ไม่เปลี่ยนแปลง

## 6. CI risk scan (ubuntu-latest / macos-latest vs green-on-Windows)
Grep ทั้ง 5 diffs (`c958d0a 8e5c67f b1e9ba1 2e0b6fd 411d2ed`) หา
`sys.platform|os.name|win32|posix|shell=True|subprocess.run|os.environ\[.CI.\]` — **ไม่พบ
pattern ใหม่ที่เสี่ยง platform-specific เลย** ทุกไฟล์ที่แก้ใช้ `pathlib.Path` +
`.resolve()`/`str()` ตามเดิม (cli.py:`_live_worktree_paths_best_effort`,
worktree_manager.py:`clean_isolated`) ไม่มี hardcode path separator หรือ shell=True ใหม่
เข้ามา — ความเสี่ยง CI matrix ต่ำ

## เกณฑ์ตัดสิน — **GO**
ทุกข้อผ่านครบ: pytest 5768/0 failed, ruff+format สะอาด, lint-imports 24/0 broken,
depgraph fresh, pre-commit 6/6 passed, git status สะอาด, ตรวจ integration 2 จุดเสี่ยง
(#190×#186, #185×#187) แล้วไม่พบการตีกัน, ไม่พบความเสี่ยง CI-platform ใหม่
