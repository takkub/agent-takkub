# agent-takkub — team conventions

> **คุณคือ Lead pane?** อ่าน **`docs/lead/role-and-workflow.md`** (core file <= 4k token) ทั้งไฟล์เดี๋ยวนี้ก่อนทำอะไรต่อ (บทบาท Lead, Sizing ก่อน routing, routing table, auto-fire/propose 3 กรณี, done-handoff + long-run, direct-edit policy, anti-patterns — เรื่องเฉพาะทางแยกอยู่ที่ `docs/lead/`) ไฟล์นี้ (root CLAUDE.md) ถูก Claude Code auto-load เข้า**ทุก pane ทุก role** จึงเหลือไว้แค่กฎที่ทุกคนต้องรู้จริงๆ

Teammates: frontend · backend · mobile · devops · qa · reviewer · critic · gemini · codex · opencode · kimi · cursor · tester/analyst/designer/docs/security (secondary positions, off by default — เปิดได้จาก Settings → Roles & ตำแหน่ง toggle หรือ `takkub assign --role <name>` ตรงๆ ก็ได้ แต่ไม่ auto-spawn เว้นแต่เปิดใช้) — route ผ่าน `takkub` CLI (specialist ห้าม spawn subagent เอง เว้นแต่ Lead สั่ง task ด้วย `--mode subagent`)

## กฎที่ใช้กับทุก role (Lead + specialist ทุกคน)

> **ก่อน navigate/แก้ `src/agent_takkub/`:** god-files แตกเป็น 10 mixins แล้ว (2026-06) — อ่าน `docs/architecture/godfile-map.md` + `docs/architecture/depgraph.json` — **อย่า grep มั่วแล้วเดา** · guardrail = import-linter 25 contracts (CI)

> **Multi-provider (2026-07-09):** ทุก feature/fix ต้องรองรับ**ทุก provider** (claude/codex/gemini-agy/opencode/kimi/cursor — ProviderSpec #103) หรือระบุ gap ชัดๆ ห้ามเงียบ

> **Cross-platform (Windows ConPTY + macOS `_pty_backend`):** ทุกการเปลี่ยนแปลงต้องทำงานทั้ง 2 OS — ห้าม hardcode path/command เฉพาะ platform · CI matrix `windows-latest`+`macos-latest` ต้องเขียวทั้งคู่ก่อน merge

> **Test tiers (#485/#585 — ทดสอบของจริง ไม่ใช่เขียนไฟล์เทส):** specialist **ห้ามรัน `takkub qa-gate` เอง** — ทดสอบสิ่งที่แก้ด้วยของจริงแล้วเขียนในโน้ตว่าเห็นอะไร · เขียนไฟล์เทสใหม่เฉพาะ scope=deep / bug ที่เคยหลุด / Lead สั่ง (งาน UI ห้ามเขียน) · รันได้แค่ **targeted เฉพาะไฟล์ที่แก้** และห้าม build/รัน suite ตอนเครื่องไม่ว่าง แล้ว `takkub done` · qa-gate ครั้งเดียวท้าย batch **เฉพาะ batch ที่มีงาน deep** · รายละเอียดเต็ม → `docs/qa-gate-policy.md`

## กฎสำหรับคนพัฒนาตัว cockpit (repo นี้เท่านั้น — Lead ที่ทำ batch/release)
- **รวบ merge ทุก worktree → push ครั้งเดียว → CI รอบเดียว** — ห้ามวน merge/push/แดง/แก้ ทีละใบ · bump version + CHANGELOG อยู่ใน push เดียวกับ fix · ปิด issue ให้หมดก่อน bump
- **ก่อน push:** รัน `tests/test_*guard*.py` + `test_issues.py` + `test_core_contracts.py` **และ** ไฟล์เทสที่ grep เจอว่าอ้าง method/ฟังก์ชันที่แก้ หรือใช้ test double ของ orchestrator (`_FakeOrch|_ExitFake|SimpleNamespace`) — targeted เฉพาะไฟล์ที่แก้ไม่พอ (2.1.8 CI แดง 37 เทส) · เทสที่แตะ provider ต้องผ่านแบบตัด codex/agy/gemini ออกจาก PATH ด้วย (CI ไม่มี)
- ห้ามรัน full qa-gate ในเครื่อง ให้ CI รัน · ยืนยัน HEAD/merge/CI ด้วย `rtk proxy git …` / `gh run list --commit $(git rev-parse HEAD) --json name,status,conclusion` — **อ่านเฉพาะแถว `"name":"ci"` และรอ completed** (security/codeql/graph เขียวได้ทั้งที่ `ci` แดง — 2.1.14 publish ทั้งที่แดงเพราะดูผิดแถว · rtk แสดง git log เพี้ยน, `gh run watch` โกหก exit 0)
- แก้ `remote/static/app.js|index.html` ต้อง bump `CACHE_NAME` ใน `sw.js` (PWA cache-first) · storage V2: 1 domain 1 โฟลเดอร์ ไม่มีไฟล์ลอย ไม่ลบข้อมูล · Lead พิมพ์ช้า = GIL convoy จาก worker thread → `py-spy dump` ไม่ใช่ดู CPU
