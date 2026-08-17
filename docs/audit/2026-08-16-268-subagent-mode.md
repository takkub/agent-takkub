# #268 — `assign --mode subagent`

## สรุป implementation

- เพิ่ม `takkub assign --mode pane|subagent`; ไม่ใส่ flag = `pane` และยังผ่าน async pane/spawn/delivery path เดิม
- `subagent` ลงทะเบียน assignment + Task Ledger และเขียน task capsule โดยไม่สร้าง `AgentPane`/PTY
- Lead dispatch capsule ผ่าน native subagent tool ของ provider ปัจจุบัน; capsule ระบุ cwd, role, ข้อจำกัด same-provider และ completion command
- เพิ่ม `takkub subagent-done --role <role> [--fail] "<summary>"` เพื่อส่งผลเข้า completion sinks เดิม: ledger status, session decision note, Lead inbox/notice, `takkub wait`, shard aggregate, worktree finalize, hot note และ `agentDone`
- fan-out subagent รองรับ `--shards 1..20`; pane ยังคง cap เดิม 1..8
- `--model` และ `--plan` ถูก reject ใน subagent mode เพื่อไม่สร้างคำสัญญาที่ native child ทำไม่ได้; provider/model context มาจาก parent เสมอ
- worktree mode ยังใช้ได้: cockpit สร้าง worktree ก่อนเขียน capsule และ finalize ตอน `subagent-done`
- `routing_planner.classify()` เพิ่ม `suggested_mode`/`mode_reason`; scan/audit/search/triage/fan-out แนะนำ subagent ส่วน implementation/intervention และ cross-model แนะนำ pane ทั้งหมดเป็น suggestion เท่านั้น

## ข้อดี / ข้อเสียที่รักษาตาม issue

ข้อดี: ตัด pane/PTY/provider boot และปัญหา delivery/ready-gate ออกจากเส้นทางงานทรง scan; ผล native child กลับ parent โดยตรง ขณะเดียวกัน cockpit ยังมี audit trail.

ข้อจำกัดที่เขียนไว้ใน capsule และเอกสาร Lead:

- user มองไม่เห็นการทำงานสดใน pane
- `takkub send` แทรกกลางทางและ cockpit permission approval ใช้ไม่ได้
- token งานไม่ได้ลด และผลยังกลับเข้า context ของ parent
- native child ใช้ provider เดียวกับ parent เสมอ จึงห้ามเคลมว่าแทน cross-check ต่างโมเดล/provider ได้

## จุดกฎที่แก้ให้สอดคล้อง

Operational prompt/policy sources:

1. `.claude/agents/*.md` ครบ 16 role: analyst, backend, codex, critic, cursor, designer, devops, docs, frontend, gemini, kimi, mobile, opencode, qa, reviewer, security
2. `CLAUDE.md`
3. `.agents/rules/no-gemini-subagents.md`
4. `src/agent_takkub/codex_agents_md.py` (รวม managed `AGENTS.md` template)
5. `src/agent_takkub/custom_roles.py`
6. `src/agent_takkub/lead_context.py`
7. `src/agent_takkub/provider_spec.py` (`task_notice_preamble`)
8. `src/agent_takkub/orchestrator_text.py` (Codex rewrite explanation)
9. `src/agent_takkub/spawn_engine.py` (`Task` tool hard-block explanation)
10. `docs/lead/role-and-workflow.md`
11. `docs/lead/patterns.md`
12. `docs/lead/cli-reference.md`
13. `src/agent_takkub/lead_wait.py` (pending native child ไม่ถูกตีเป็น never-spawned; implicit wait รวม subagent)

`pane_guard.py` ตรวจแล้ว: ไม่มี rule ที่บล็อก native subagent/Task tool; มันตรวจเฉพาะ Bash command families (browser driver, disk scan, host-destructive process kill และ pip editable). ดังนั้นไม่แก้ไฟล์นี้. Hard block จริงอยู่ที่ `spawn_engine._teammate_disallowed_tools()` และยังคงเปิดเฉพาะ pane-mode teammate; subagent mode วิ่งใต้ Lead process จึงไม่เข้ากิ่งนี้.

Test/example text ที่อ้างกฎเดิมถูกปรับใน `test_codex_task_rewrite.py`, `test_orchestrator_auto_respawn_replay.py`, `test_task_ledger.py`, `test_codex_agents_md.py`; historical plan/review สองไฟล์ถูกตีป้ายว่าเป็นพฤติกรรมก่อน #268 แทนการปล่อยให้ดูเหมือน policy ปัจจุบัน.

## Verification

- targeted CLI/planner/subagent tests: ผ่าน
- targeted CLI server + role-template/Codex rewrite + Lead context + pane guard tests: ผ่าน
- Ruff check และ format check เฉพาะไฟล์ที่แตะ: ผ่าน
- ไม่รัน full suite ตามข้อกำหนด task
- ใช้ interpreter จาก repo `.venv` พร้อม prepend worktree `src` ผ่าน `PYTHONPATH` เพื่อไม่แก้ shared editable install (#202)

## ไฟล์ที่ตั้งใจไม่แตะ

- `agent_pane.py`
- `pty_session.py`
- state/list-status backend ที่ #263 กำลังทำคู่ขนาน
