# agent-takkub — team conventions

> **คุณคือ Lead pane?** อ่าน **`docs/lead/role-and-workflow.md`** ทั้งไฟล์เดี๋ยวนี้ก่อนทำอะไรต่อ (บทบาท Lead, routing table, propose/confirm, done-handoff, CLI reference, anti-patterns — ทุกอย่างที่เคยอยู่ในไฟล์นี้ ย้ายไปที่นั่นหมดแล้ว token diet 2026-08-16 #267) ไฟล์นี้ (root CLAUDE.md) ถูก Claude Code auto-load เข้า**ทุก pane ทุก role** จึงเหลือไว้แค่กฎที่ทุกคนต้องรู้จริงๆ

Teammates: frontend · backend · mobile · devops · qa · reviewer · critic · gemini · codex · opencode · kimi · cursor · tester/analyst/designer/docs/security (secondary positions, off by default — เปิดได้จาก Settings → Roles & ตำแหน่ง toggle หรือ `takkub assign --role <name>` ตรงๆ ก็ได้ แต่ไม่ auto-spawn เว้นแต่เปิดใช้) — route ผ่าน `takkub` CLI (specialist ห้าม spawn subagent เอง เว้นแต่ Lead สั่ง task ด้วย `--mode subagent`)

## กฎที่ใช้กับทุก role (Lead + specialist ทุกคน)

> **ก่อน navigate/แก้ `src/agent_takkub/`:** god-files แตกเป็น 10 mixins แล้ว (2026-06) — อ่าน `docs/architecture/godfile-map.md` + `docs/architecture/depgraph.json` — **อย่า grep มั่วแล้วเดา** · guardrail = import-linter 25 contracts (CI)

> **Multi-provider (2026-07-09):** ทุก feature/fix ต้องรองรับ**ทุก provider** (claude/codex/gemini-agy/opencode/kimi/cursor — ProviderSpec #103) หรือระบุ gap ชัดๆ ห้ามเงียบ

> **Cross-platform (Windows ConPTY + macOS `_pty_backend`):** ทุกการเปลี่ยนแปลงต้องทำงานทั้ง 2 OS — ห้าม hardcode path/command เฉพาะ platform · CI matrix `windows-latest`+`macos-latest` ต้องเขียวทั้งคู่ก่อน merge

> **Test tiers (#485 — gate ครั้งเดียวตอนจบ batch, ห้ามรันถี่):** specialist **ห้ามรัน `takkub qa-gate` เอง** — เขียนเทสกันถอย (#478) + รันได้แค่ **targeted เฉพาะไฟล์ที่แก้** แล้ว `takkub done` · full gate = qa pane ทำครั้งเดียวท้าย batch (`takkub qa-gate --auto`) ก่อน merge/push · รายละเอียดเต็ม (Node/Python ต่างกันยังไง, ทำไม full tier ถึงจำเป็น) → `docs/qa-gate-policy.md`
