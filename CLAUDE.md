# agent-takkub — team conventions

> **คุณคือ Lead pane?** อ่าน **`docs/lead/role-and-workflow.md`** ทั้งไฟล์เดี๋ยวนี้ก่อนทำอะไรต่อ (บทบาท Lead, routing table, propose/confirm, done-handoff, CLI reference, anti-patterns — ทุกอย่างที่เคยอยู่ในไฟล์นี้ ย้ายไปที่นั่นหมดแล้ว token diet 2026-08-16 #267) ไฟล์นี้ (root CLAUDE.md) ถูก Claude Code auto-load เข้า**ทุก pane ทุก role** จึงเหลือไว้แค่กฎที่ทุกคนต้องรู้จริงๆ

Teammates: frontend · backend · mobile · devops · qa · reviewer · critic · gemini · codex · opencode · kimi · cursor — spawn/route ผ่าน `takkub` CLI (Lead เท่านั้น, specialist ห้าม spawn subagent)

## กฎที่ใช้กับทุก role (Lead + specialist ทุกคน)

> **ก่อน navigate/แก้ `src/agent_takkub/`:** god-files แตกเป็น 10 mixins แล้ว (2026-06) — อ่าน `docs/architecture/godfile-map.md` (method→module + hidden string/socket edges) + `docs/architecture/depgraph.json` (import map, auto-refresh ทุก commit) — **อย่า grep มั่วแล้วเดา** · guardrail = import-linter 23 contracts (enforced in CI, not just local pre-commit)

> **Multi-provider (user directive 2026-07-09):** ทุก feature/fix ต้องคำนึงถึง**ทุก provider** (claude/codex/gemini-agy/opencode/kimi/cursor + อนาคต — ProviderSpec #103): engine feature ใหม่ต้องทำงานกับ pane ที่ไม่ใช่ claude ด้วยหรือระบุ gap ชัดๆ · wording อย่าผูก claude-only โดยไม่มี fallback · claude-only shortcut ที่เลี่ยงไม่ได้ต้อง flag เข้า #103 ห้ามเงียบ

> **Cross-platform (Windows ConPTY + macOS `_pty_backend`):** ทุกการเปลี่ยนแปลงต้องทำงานทั้ง 2 OS — ห้าม hardcode path/command เฉพาะ platform (ใช้ `pathlib.Path`); platform-specific ต้อง gate `sys.platform` + มี branch อีกฝั่งเสมอ · CI = matrix `windows-latest` + `macos-latest` **ทั้งคู่ต้องเขียว**ก่อน merge
>
> **Test tiers (user directive 2026-07-09 — ห้ามเทสเปลือง):** งานย่อยกลางทางรัน **targeted tests เฉพาะที่แตะ** — **full suite รันครั้งเดียวที่ qa batch gate** ก่อน merge/push (fake ที่ signature drift จะ raise ใน QTimer slot → PyQt6 abort เงียบ exit 127 ที่ targeted run ไม่จับ) · ข้อยกเว้นเดียว: refactor ที่เคลม behavior-neutral (proof = suite เดิมเขียวไม่แก้ expected values)
