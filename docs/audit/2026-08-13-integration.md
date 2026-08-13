# Integration รวม 8 branch — 2026-08-13

## สรุป

รวมทั้งหมด **8 branch** เข้า branch เดียว: **`wt/devops-1786610975`** (ใช้ pane worktree branch เดิมเป็น integration branch ตามที่ Lead แก้คำสั่ง — ไม่ได้สร้าง `integration/2026-08-13` ใหม่) แตกจาก `main` ที่ commit `df1f65f` (ยืนยันก่อนเริ่มว่า branch ตรงกับ main tip แล้ว ไม่ต้อง rebase)

ทุก branch merge สำเร็จด้วย `git merge --no-ff` **ไม่มี conflict marker หลงเหลือเลยสักไฟล์** — grep `^<<<<<<<|^=======$|^>>>>>>>` ทั่ว `src/ tests/ docs/` หลังทุกกลุ่มว่างเปล่า Git `ort` strategy auto-merge ได้เองทุกจุดที่คาดว่าจะชน เพราะ diff อยู่คนละ hunk/บรรทัดกัน

## ลำดับการรวม + commit

| กลุ่ม | Branch | Merge commit | ไฟล์ที่คาดว่าชน | ผลจริง |
|---|---|---|---|---|
| A.1 | `wt/devops-1786606983` (#164, #166) | `ed36638` | — | clean, no touch |
| A.2 | `wt/devops-1786608412` (#163, #165) | `9c5a5bf` | `orchestrator.py` | auto-merged (คนละ hunk) |
| B.1 | `wt/backend-1786607752` (usage abstraction + API + UI) | `f5a5ed1` | — | clean |
| B.2 | `wt/backend-3-1786608537` (remote mirror fix) | `d102249` | `remote/api.py`, `remote/static/app.js`, `tests/test_remote_api.py`, `cli.py`, `cli_server.py`, `depgraph.json` | auto-merged ทุกไฟล์ (คนละ hunk) |
| C | `wt/frontend-1786602192` (New Role redesign + autoskills bridge + security-flag) | `f28ece2` | `depgraph.json` | auto-merged |
| D.1 | `wt/backend-1786606932` (docs: provider usage survey) | `70ac92a` | — | clean (ไฟล์ใหม่) |
| D.2 | `wt/gemini-1786606933` (docs: provider usage design) | `bd00911` | — | clean (ไฟล์ใหม่) |
| D.3 | `wt/qa-1786609656` (CI determinism: isolate custom_roles path + PATH allowlist assertion) — เพิ่มเข้ามาระหว่างทางตามที่ Lead สั่งเพิ่ม | `7b60f63` | — | clean |

**ไม่มีจุดไหนต้องตัดสินใจทิ้งฝั่งใดฝั่งหนึ่ง** — ทุก feature จากทั้ง 8 branch อยู่ครบใน merge tree

## depgraph.json

`docs/architecture/depgraph.json` ถูก auto-merge (ort) ผ่านได้เองในทุก merge ที่แตะไฟล์นี้ (B.2, C) แต่ตามกฎ "auto-gen conflict → regenerate ไม่แก้มือ" — รัน `tools/gen_import_graph.py` ซ้ำหลังรวมครบทั้ง 8 branch เพื่อความชัวร์ (module_count=141) ได้ diff เล็กน้อย (3 บรรทัดเปลี่ยน) เทียบกับ auto-merge ผลลัพธ์ — commit ทับด้วยไฟล์ regenerate แล้ว

## Targeted tests (รันหลังรวมแต่ละกลุ่ม)

| กลุ่ม | ไฟล์เทสที่รัน | ผล |
|---|---|---|
| A | `test_send_unknown_role_message.py`, `test_task_ledger.py`, `test_task_reconcile_close_cli.py`, `test_task_reconcile_orchestrator.py`, `test_done_evidence.py`, `test_orchestrator_stall.py`, `test_pending_done_notice_visibility.py` | ✅ 133 passed |
| B | `test_provider_usage.py`, `test_remote_api.py`, `test_remote_http_server.py`, `test_remote_mirror_diagnostics.py` | ✅ ~177 passed |
| C | `test_autoskills_installer.py`, `test_settings_window.py` | ✅ passed (มี 2 skip ปกติ — Qt-dependent) |
| D | `test_headless_entrypoint.py`, `test_orchestrator_env_allowlist.py` | ✅ 22 passed (ตัวนี้คือ 2 ไฟล์ที่ qa แก้ isolate `custom_roles` registry path เพื่อไม่ให้ full suite แดงบนเครื่อง dev ที่มี `~/.takkub` จริง) |

ไม่ได้รัน full suite — ตาม policy "targeted mid-flight, full suite ครั้งเดียวที่ qa batch gate"

## Lint / architecture gates

- `lint-imports` → **23/23 contracts kept, 0 broken**
- `ruff check src/ tests/` → **All checks passed**

## ข้อควรระวัง / สิ่งที่ Lead ควรรู้ก่อนไปต่อ

- Branch นี้ (`wt/devops-1786610975`) มี merge commit ภายในของมันเองอยู่ก่อนแล้ว 2 ตัว (`2f1aaf2` desktop-usage-UI→provider_usage, `1cdecac` autoskills_installer→New-Role-redesign) — เป็น merge ที่ทำไว้ก่อนหน้าภายใน branch ต้นทางเอง ไม่ใช่ของ integration รอบนี้ ไม่ต้องแก้อะไรเพิ่ม
- `wt/qa-1786609656` (branch ที่ 8) ยังไม่เสร็จสมบูรณ์ตอนเริ่มงาน — Lead แจ้งเพิ่มเข้ามาระหว่างทางว่า "พร้อมแล้ว" ก่อนถึงกลุ่ม D จึงรวมเข้าไปด้วยตามคำสั่งล่าสุด
- ยังไม่ได้รัน full pytest suite / QA gate — รอ Lead ส่งต่อให้ qa ตาม flow ปกติ

## Branch สุดท้ายที่มีของครบ

**`wt/devops-1786610975`**
Latest commit: **`7c0660e`** (chore(integration): regenerate depgraph.json + integration report — commit ปิดท้ายที่มีของครบทั้ง 8 branch)
