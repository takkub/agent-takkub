---
title: Roadmap re-audit (A1–A7, B1–B4) — ground-truth before dev plan
date: 2026-07-10
author: lead (Claude)
purpose: >
  User สั่งงาน 10 ข้อ (บางข้อทำไป session ก่อน) + เพิ่ม A7. Re-audit ทุกข้อด้วย
  หลักฐาน file:line ก่อนแตกแผน dev. รอบนี้แก้ที่เคยสรุปผิด (A2, A5).
  ต่อไป codex + gemini cross-check ทุก claim ในไฟล์นี้ (หา false-positive/มองข้าม).
---

# Roadmap re-audit — 2026-07-10

**Legend:** ✅ done · ⚠️ partial/มีปัญหา · ❌ not built · ⛔ cancelled

| # | Item | Status | Evidence (file:line) |
|---|------|--------|----------------------|
| **A1** | File-based task delivery — pane อ่านไฟล์แทนถูก paste ยาวๆ | ✅ | `orchestrator_text.py:434 _task_handoff_pointer` เขียน long task → `RUNTIME_DIR/tasks/<project>/<date>/<HHMMSS>-<role>.md` แล้ว paste pointer `orchestrator_text.py:464`. **Caveat:** เขียนไฟล์เฉพาะ task ยาว ≥ `TASK_HANDOFF_THRESHOLD` (สั้น = paste ตรง). Multi-provider OK (wording "file-read tool" ไม่ผูก claude). Artifacts อยู่ใต้ `RUNTIME_DIR` ไม่ทิ้งในโปรเจคจริง. |
| **A2** | เอา fanout cap ที่จำกัด parallel ออก | ✅ (code) ⚠️ (doc) | Lead planning prompt **ไม่มี** numeric cap แล้ว → qualitative wave advisory แทน (`lead_context.py:467-494`, `exec_mode.py:14-19`). `machine_fanout_cap()` เหลือไว้เป็น telemetry component ของ `machine_total_pane_cap()` (non-blocking warn เท่านั้น `orchestrator.py:3486`). **⚠️ CLAUDE.md:75 STALE** — ยังเขียน "Cap K ≤ machine_fanout_cap()" ขัดกับ code → ต้องแก้ doc. |
| **A3** | Lead draft-hold — พิมพ์ค้างไม่โดนส่งไปตอน pane report done | ⚠️ | Mechanism ครบ: `lead_draft_state.py` (state machine #3/#108) + wiring `lead_inbox.py:308-325,462,788,952,1024,1061` fed by `orchestrator.py:3964 _on_pane_input`. **แต่ user รายงานว่ายังหลุด** → ต้อง repro + debug (สงสัย paint-gap / draft_len drift / bracketed-paste). |
| A4 | /remote-control auto-bridge | ⛔ | user สั่งตัดทิ้ง (ลบหมดแล้ว commit 28136df) |
| **A5** | QA ส่ง screenshot/หลักฐานให้ Lead | ✅ | `orchestrator.py:1591 _scan_done_evidence` scan artifacts dir หา `.png/.jpg/...` ใหม่กว่า assign_ts → แนบ `📸 evidence: <paths>` เข้า done notice (`:1741`), เตือน `⚠ no screenshot evidence` ถ้า qa/critic/designer ไม่มีรูป (`:1634`). issue #5. |
| **A6** | UI เพิ่ม role + กำหนด skill + คลัง skill + default | ❌ | ไม่มีไฟล์ role-builder/skill-catalog dialog. มีแค่ `roles.py` (registry), `role_memory.py`, `skill_audit.py`, Tools chip (MCP/plugins policy). **Net-new feature.** |
| **A7** | **(ใหม่)** ทุก assign เขียน task file หลักฐาน folder เดียว + date/meta ครบ → done = flip ✅ checklist | ⚠️ | A1 คลุมบางส่วน: long task เขียนไฟล์ `runtime/tasks/<project>/<date>/`. **ขาด:** (1) เขียน**ทุก** task ไม่ใช่แค่ยาว (2) metadata ครบ (role/cwd/ts/status header) (3) **flip ✅ เมื่อ takkub done** (ledger/checklist) (4) folder รวมดูง่าย. = ต่อยอดจาก A1. |
| **B1** | Close project (web) | ✅ | `app.js:405 closeProject` |
| **B2** | Q&A/brainstorm web — เลือกข้อ + comment | ⚠️ | comment ✅ (composer free-text control mode `app.js:1163`). เลือกข้อ = แค่ generic quick-reply chips (`app.js:905 STANDARD_QUICK_REPLIES` = ok/ไม่เอา/ขอดูแผน) + banner `blocked_on_picker` (`:1122`). **ขาด:** tappable per-option chips ของ AskUserQuestion จริง (ตอนนี้แค่เตือนให้ตอบ). |
| **B3** | Resume + เลือก session (web) | ✅ | resume/session picker sheet (W3) `app.js:1174+`, `updateResumeButtonVisibility` `:1171` |
| **B4** | Pulse แสดง Lead (working/idle/done) | ✅ | pulse โชว์ lead role + runtime |

## สรุปงานเหลือจริง (สำหรับ dev plan)
1. **A3** — debug draft-hold (user เจอสด, แก้ก่อน) · Windows+Mac
2. **A6** — role/skill manager UI + คลัง skill (ใหญ่สุด net-new) · multi-provider
3. **A7** — task ledger + checklist (ต่อยอด A1) · Windows+Mac
4. **B2** — per-option picker ฝั่ง web (ต่อยอด chips)
5. (A2 doc) — sync CLAUDE.md:75 ให้ตรง code (แก้ 1 บรรทัด)

**Done แล้ว (ไม่ต้องทำ):** A1, A2(code), A5, B1, B3, B4.

## สิ่งที่ codex/gemini ต้อง cross-check
- ยืนยัน/แย้งทุก file:line ข้างบน (รอบก่อน Lead สรุป A2/A5 **ผิด** — อย่าเชื่อ Lead)
- A3: หา root cause ว่าทำไม draft-hold ยังหลุด (อ่าน `lead_draft_state.py` + `lead_inbox.py` flow)
- A7 vs A1: มี overlap อะไรที่ reuse ได้ / ต้องเพิ่มอะไรบ้าง
- ข้อไหนที่เคลม ✅ แต่จริงๆ มี gap (โดยเฉพาะ cross-platform Win/Mac + multi-provider)
