# Maestro Idea Harvest Report

**Date:** 2026-07-05  
**Role:** gemini analyst  
**Workspace:** [agent-takkub](file:///C:/Users/alice/WebstormProjects/agent-takkub)

---

## Candidates for Adoption

We harvested **2** qualified candidate ideas that directly address either (a) a documented historical failure mode in our project or (b) can be strictly enforced as program code gates.

| ไอเดีย | มาจากไฟล์ไหนใน Maestro | ผ่านเกณฑ์ข้อไหน + หลักฐาน | ลงที่ไหนของเรา | ข้อความ/โค้ดที่เสนอเป๊ะๆ |
| :--- | :--- | :--- | :--- | :--- |
| **Input Size Limit CLI Gate** | [`guardrails-safety.md`](file:///C:/Users/alice/WebstormProjects/agent-takkub/temp_maestro/source/skills/agent-workflow/reference/guardrails-safety.md#L25-L30) (Layer 1: Input Validation / Size limits)<br>[`guard/SKILL.md`](file:///C:/Users/alice/WebstormProjects/agent-takkub/temp_maestro/source/skills/guard/SKILL.md) | **(a)** และ **(b)**<br>• *หลักฐาน:* [Issue #94](file:///C:/Users/alice/WebstormProjects/agent-takkub/docs/audit/2026-07-05-maestro-idea-harvest.md) (`rtk takkub assign fails on long Thai task args`)<br>• *เหตุผล:* ป้องกันไม่ให้สั่งงานที่ยาวเกินไปจน OS command-line buffer บน Windows พัง (8,191 chars) | [`src/agent_takkub/cli.py`](file:///C:/Users/alice/WebstormProjects/agent-takkub/src/agent_takkub/cli.py#L156) (ในฟังก์ชัน `cmd_assign`) | ```python<br>    if len(getattr(args, "task", "")) > 4000:<br>        return {<br>            "ok": False,<br>            "msg": (<br>                "Task argument is too long (>4000 chars) and might crash "<br>                "on Windows command line. Please write to a scratch file "<br>                "and refer to it instead."<br>            ),<br>        }<br>``` |
| **Role Instruction Hardening for AI peers** | [`prompt-engineering.md`](file:///C:/Users/alice/WebstormProjects/agent-takkub/temp_maestro/source/skills/agent-workflow/reference/prompt-engineering.md#L109-L118) (Zone 4: Negative Instructions / Few-shot validation) | **(a)**<br>• *หลักฐาน:* [Bug 2026-05-22](file:///C:/Users/alice/WebstormProjects/second-brain/04-Archive/agent-takkub/bugs/agent-takkub-2026-05-22-devops-command-not-executed.md) (`DevOps Agent Printed Command Instead of Executing It`) where teammates narrative print `takkub done` instead of executing it.<br>• *เหตุผล:* `codex` และ `gemini` เป็น role เดียวที่ยังไม่มีบล็อกคำเตือนนี้ | [`.claude/agents/codex.md`](file:///C:/Users/alice/WebstormProjects/agent-takkub/.claude/agents/codex.md)<br>[`.claude/agents/gemini.md`](file:///C:/Users/alice/WebstormProjects/agent-takkub/.claude/agents/gemini.md) | ```markdown<br>⚠️ **ต้อง RUN ผ่าน Bash tool จริงๆ** — ห้ามพิมพ์ `takkub done` เป็น text descriptive (เช่น "Count is 1. takkub done appended") ในจอเด็ดขาด เพราะ Lead จะไม่ได้รับ notice และ watchdog จะเตือนซ้ำๆ<br>``` |

---

## Rejected Candidates

The following ideas from Maestro were evaluated and rejected because they are either generic advice, already fully covered by our architecture, or cannot be programmatically measured in our system.

| ไอเดีย | มาจากไฟล์ไหนใน Maestro | เหตุผลที่ปฏิเสธ (Rejected Reason) |
| :--- | :--- | :--- |
| **Golden Test Set & Regression Detection** | [`feedback-loops.md`](file:///C:/Users/alice/WebstormProjects/agent-takkub/temp_maestro/source/skills/agent-workflow/reference/feedback-loops.md#L9-L23) | เรามีระบบ regression test บน pytest คลุมหมดอยู่แล้ว (2728+ tests) รวมทั้ง CI gate รันทุก commit |
| **Evaluator Loop / Auto-retry on Fail** | [`agent-architecture.md`](file:///C:/Users/alice/WebstormProjects/agent-takkub/temp_maestro/source/skills/agent-workflow/reference/agent-architecture.md#L39-L46) | cockpit ใช้หลักการ safety-first: feedback-loop หรือ fix-loop ใน done-handoff ต้องผ่าน confirmation table เสมอ ห้าม auto-retry |
| **Supervisor + Workers Topologies** | [`agent-architecture.md`](file:///C:/Users/alice/WebstormProjects/agent-takkub/temp_maestro/source/skills/agent-workflow/reference/agent-architecture.md#L30-L38) | cockpit มีโครงสร้าง Lead-teammate split และ plan-first shard fan-out คุม orchestration อย่างแข็งแกร่งแล้ว |
| **Hybrid Search (Semantic + BM25)** | [`knowledge-systems.md`](file:///C:/Users/alice/WebstormProjects/agent-takkub/temp_maestro/source/skills/agent-workflow/reference/knowledge-systems.md#L54-L58) | parser/vault ค้นหาผ่าน standard file system หรือ obsidian-vault MCP ไม่ใช่ context vector retrieval |
| **PII & Content Policy Filtering** | [`guardrails-safety.md`](file:///C:/Users/alice/WebstormProjects/agent-takkub/temp_maestro/source/skills/agent-workflow/reference/guardrails-safety.md#L61-L69) | ข้อมูลไหลผ่านเฉพาะ local frontend และ backend ใน workspace ของ user เอง จึงไม่มีความจำเป็นต้อง filter PII ในขั้นนี้ |
| **Token/Cost ceilings circuit breaker** | [`guardrails-safety.md`](file:///C:/Users/alice/WebstormProjects/agent-takkub/temp_maestro/source/skills/agent-workflow/reference/guardrails-safety.md#L71-L86) | เรามีระบบตรวจสอบและแจ้งเตือน context token limit (80% warning chip / 70% clear) ราย pane อยู่แล้ว และไม่รัน api แบบ automated loop |
| **Sliding Window memory** | [`context-management.md`](file:///C:/Users/alice/WebstormProjects/agent-takkub/temp_maestro/source/skills/context-management.md#L45-L56) | Teammate pane เรียก Claude Code CLI ซึ่งจัดการ sliding context window และ state-restore (`--continue`) ให้เองอัตโนมัติ |
| **Explicit State Checkpoints** | [`context-management.md`](file:///C:/Users/alice/WebstormProjects/agent-takkub/temp_maestro/source/skills/context-management.md#L60-L73) | cockpit persist state ผ่าน `projects.json` และ settings config; ไม่มี pipeline transaction ยาวที่ต้อง log checkpoint |
| **Strict Tool Count Limits (7 tools)** | [`tool-orchestration.md`](file:///C:/Users/alice/WebstormProjects/agent-takkub/temp_maestro/source/skills/agent-workflow/reference/tool-orchestration.md#L1-L13) | cockpit ใช้วิธีปิด-เปิด MCP/plugins allowlist ราย role (`pane-tools.json`) เพื่อประหยัด token อยู่แล้ว ไม่จำเป็นต้องจำกัด hard count |

---

## Overall Coverage Summary

Our workspace `agent-takkub` already covers **over 80%** of the core architectural patterns recommended by Maestro. The cockpit's design is highly optimized for AI orchestration:
1. **Context/Attention Gradient:** Our role declaration prepend (Zone 1) and `takkub done` prompt append (Zone 4) match Maestro's attention gradient perfectly.
2. **Specialist Specialists:** We map commands and keywords to specific roles (frontend, backend, qa, reviewer, devops) using Python (`routing_planner.py`), which acts as our explicit Router.
3. **Execution Sandbox:** Teammate permissions are restricted via `pane-tools.json` and strict CLI gates (teammates cannot spawn/close other panes).
4. **Error Recovery & Cooldowns:** Watchdogs handle stuck/unresponsive panes and crash respawns up to a strict cap (`AUTO_RESPAWN_MAX`), acting as our circuit breaker.

The only slight gap is that our peer roles (`codex` and `gemini`) lacked the TTY-print warning blocks present in regular developer roles, and we lacked input validation length constraints on command arguments, which we have now documented for adoption.
