# V2 — Reuse vs Rewrite Matrix

> Phase 0 deliverable · baseline `9c6b56d` (1.0.75) · 2026-08-19
> คู่กับ [`CURRENT_ARCHITECTURE_AUDIT.md`](CURRENT_ARCHITECTURE_AUDIT.md)

**นิยามคำตัดสิน**

| คำ | ความหมาย |
|---|---|
| **REUSE** | ใช้ต่อตามเดิม ไม่แตะ · Core V2 เรียกใช้ตรงๆ |
| **WRAP** | โค้ดเดิมไม่แก้ แต่ห่อด้วย adapter/façade ของ V2 แล้วให้ core คุยผ่าน adapter เท่านั้น |
| **EXTEND** | โค้ดเดิมเป็นฐานที่ถูกต้อง แต่ต้องเติม field/ความสามารถ |
| **NEW** | ไม่มีของเดิม ต้องสร้างใหม่ใน `core/` |
| **REPLACE** | มีของเดิมแต่ผิดหลักการ V2 ต้องเลิกใช้หลัง migrate |

---

## 1. Execution Layer

| Component | ของเดิม | คำตัดสิน | เหตุผล |
|---|---|---|---|
| PTY / process | `pty_session.py` (1,871), `_pty_backend.py`, `_win_console.py` | **REUSE** | เสถียรที่สุดในระบบ · ready-marker state machine + paste-swallow recovery + cross-platform ที่พิสูจน์แล้ว การเขียนใหม่คือความเสี่ยงล้วน ไม่มี upside |
| Pane | `agent_pane.py`, `agent_pane_model.py`, `terminal_widget.py`, `headless_pane.py` | **REUSE** | `agent_pane_model` แยก headless ได้แล้ว (มี import contract บังคับ) |
| Pane registry / lifecycle | `SpawnEngineMixin` ใน `spawn_engine.py` | **WRAP** | เป็นเจ้าของ spawn จริง แต่ V2 ต้องแทรก account/model selection ก่อน argv → ห่อด้วย `ProviderAdapter.create_session()` |
| Worktree | `worktree_manager.py` (1,511) | **REUSE** | leaf module มี contract บังคับอยู่แล้ว · V2 แค่เพิ่ม ownership metadata (project/task/agent) เป็น field |
| Project registry + UI | `projects.json`, `project_nav.py`, `project_tab.py`, `project_wizard.py` | **REUSE** (+ EXTEND เฉพาะ id) | UI ดีอยู่แล้ว · V2 เพิ่ม `project_id` ที่เสถียรเพื่อใช้เป็น path key ของ `projects/<id>/` |
| Task identity | `ps.task_id = uuid4()` ใน `_assign_dispatch` | **EXTEND** | มี id แล้วแต่เกิดกลางทาง ไม่ถูกเก็บถาวร → ยกขึ้นเป็น `Task` object ที่ persist |
| Task Ledger | `task_ledger.py` (553) | **REUSE** | markdown-first เป็น UX ที่ user ใช้จริง · V2 อ่าน/เขียนผ่านมันต่อ ไม่แทน |
| Task delivery | `task_delivery.py`, `orchestrator_text._task_handoff_pointer` | **REUSE** | idempotency + expiry + pane ownership ถูกออกแบบมาแก้ปัญหา PTY โดยเฉพาะ · V2 **ห้าม** สร้าง delivery path ใหม่ |
| Subagent mode | `_register_subagent` / `subagent_done` | **REUSE** | มี capsule + completion sink ครบแล้ว · V2 แค่เติม context เข้า capsule |
| Remote | `remote/` ทั้งโฟลเดอร์ | **REUSE** | bolt-on แยกขาดด้วย import contract · ไม่ควรลากเข้ามาใน core |

---

## 2. Intelligence Layer (Provider / Account / Model / Router)

| Component | ของเดิม | คำตัดสิน | เหตุผล |
|---|---|---|---|
| Provider definition | `provider_spec.ProviderSpec` + `PROVIDER_REGISTRY` | **EXTEND** | นี่คือ Provider Registry ของ V2 อยู่แล้ว 90% · ต้องเติม `transport` (cli/rest/openai-compatible/local), `auth` kinds, `adapter` id, compatibility range |
| Provider adapter | generic non-claude branch ใน `spawn_engine` (`:1723`) + claude branch hardcode | **WRAP → NEW contract** | มี "หนึ่งครึ่ง adapter" อยู่แล้ว · Phase 2 นิยาม `ProviderAdapter` Protocol แล้วให้ทั้ง 2 branch กลายเป็น `CliProviderAdapter` + `ClaudeCliAdapter` |
| Provider enable/disable | `provider_state.py` | **REUSE** | ตรงกับ ProviderDefinition.enabled ของ V2 |
| Provider usage/quota | `provider_usage.py` (734) | **REUSE** | `ProviderUsage` ครอบ 6 provider แล้ว · เป็น input ตรงๆ ของ quota-aware selector |
| Provider install/health | `provider_install.py`, `doctor.py` | **EXTEND** | doctor มี 30+ check แล้ว · เติม provider-version + compatibility verdict |
| **Account** | `user_profile.py` (claude-only, per-project) | **REPLACE** (แต่ migrate ข้อมูลเข้า) | หลัก V2 คือ Provider ≠ Account · ของเดิมทำได้แค่ claude และ scope ผิด → กลายเป็น `ProviderAccount` แถวแรกๆ ของ registry ใหม่ |
| Account Pool / Selector | — | **NEW** | ไม่มีของเดิม |
| Model registry | `provider_models.py`, `role_models.py` | **EXTEND** | เก็บแค่ string id → เติม context window / capability / cost class · **ข้อมูลเดิมต้อง migrate เป็น account-scoped model override** |
| Model Profile | — | **NEW** | agent ต้องขอ capability ไม่ใช่ชื่อ model |
| Router | `provider_config.effective_provider_for()` (static map + fallback) | **REPLACE** | ของเดิมเป็น mapping ไม่ใช่ routing (ไม่มี quota/cooldown/health/concurrency) · เก็บไว้เป็น `StaticRoutingPolicy` ตัวหนึ่งใน router ใหม่เพื่อ backward-compat |
| Routing planner | `routing_planner.py` (787) | **REUSE — คนละเรื่อง** | ตัวนี้คือ *task→role classification* ของ Lead ไม่ใช่ provider routing · **อย่าสับสนสองอันนี้** |

---

## 3. Capability Layer

| Component | ของเดิม | คำตัดสิน | เหตุผล |
|---|---|---|---|
| MCP policy | `pane_tools_policy.py` | **EXTEND** | role→server allowlist ใช้ได้ · เติม scope (global/project/agent/session) ตาม V2 |
| MCP wire translation | `mcp_bridge.py` (412) | **REUSE** | นี่คือหัวใจของ "provider ทุกเจ้าใช้ capability ชุดเดียว" ที่ทำเสร็จแล้วจริง |
| Shared MCP set | `shared_dev_tools.py` (1,312) | **REUSE** | รวม browser-profile isolation ต่อ pane ไว้แล้ว |
| Skill scan/policy | `skill_scan.py`, `skill_policy.py` | **REUSE** | logic ถูก · ปัญหาอยู่ที่ *path* ไม่ใช่ logic |
| Skill store path | `SKILLS_DIR = ASSETS_ROOT/.claude/skills` | **REPLACE** | ผูกชื่อ claude ทั้งที่เป็นของกลาง — user สั่งย้ายแล้ว (คิวหลัง 1.0.74) · V2: `capabilities/skills/` + `.claude/skills` เหลือเป็นแค่ surface ให้ claude discover |
| Plugin | `plugin_installer.py` | **WRAP** | ติดตั้งผ่าน `claude plugin` CLI = claude-only · ห่อด้วย `PluginManager` ที่มี claude backend เป็นตัวแรก แล้วประกาศ gap สำหรับ provider อื่น (#103) |
| Tool permission | `permission_gates.py`, `pane_guard.py` | **REUSE + EXTEND** | สองชั้น (MCP gate + shell guard) เป็นบทเรียนที่พิสูจน์แล้วว่าจำเป็น · Permission Engine ของ V2 ต้องรักษาสองชั้นนี้ไว้ ไม่ใช่ยุบเหลือชั้นเดียว |
| **Tool Gateway** | — | **NEW (scope ลด)** | intercept ทุก tool call ไม่ได้กับ CLI provider (R2) → ทำเป็น *Capability Gateway*: registry + permission + audit ที่ขอบ |

---

## 4. Cognitive Layer

| Component | ของเดิม | คำตัดสิน | เหตุผล |
|---|---|---|---|
| Conversation store | — (อ่านของ provider) | **NEW** | ช่องว่างใหญ่สุด · ต้องมีก่อนถึงจะทำ checkpoint/provider-switch ได้ |
| Transcript readers | `chatlog_scanner.py`, `src/agent_takkub/remote/notify.py` adapters, `codex/gemini/opencode_helper` | **WRAP** | เป็น "ingest adapter" ของ Conversation V2 ได้ทันที · บทเรียน schema drift (D3) ต้องขังไว้ในชั้นนี้ชั้นเดียว |
| Rolling summary | `_condense_done_note`, `_save_decision_note` | **EXTEND** | มีการย่อแล้วแต่เป็น per-done ไม่ใช่ rolling ของ conversation |
| Checkpoint | — | **NEW** | |
| Context builder | `lead_context.py` (867) + spawn-time role file staging | **EXTEND** | มีการประกอบ context ตอน spawn อยู่แล้ว · V2 ต้องขยับไป **assignment-time** และมี budget |
| Token budget | `token_meter.py`, `session_cap.py` | **REUSE** | วัด context occupancy จริงได้แล้ว (claude) · เป็น input ของ context budget |
| Role memory (L1/L2) | `role_memory.py` (637) | **REUSE เป็น source** | เป็น memory ที่ agent เขียนเอง = `agent_reported` trust level · V2 ไม่ลบ แต่จัดเป็นชั้นหนึ่งใน Brain |
| Retrieval | `bm25_search.py` (222) | **REUSE** | zero-infra BM25 + Thai trigram tokenizer · ตรงกับ "ห้าม vector-only" ของ blueprint พอดี |
| Knowledge / decisions | `runtime/knowledge/*.md`, `vault_mirror.py` | **EXTEND** | เป็น markdown ที่คนอ่านได้ · V2 เพิ่ม structured record คู่ขนาน ไม่แทน |
| Provenance / trust | `digest_facts.py` (143) | **REUSE — เป็นแม่แบบ** | แยก *cockpit-measured* ออกจาก *agent-reported* ไว้แล้ว = สิ่งที่ blueprint §06 ต้องการเป๊ะ · ขยาย enum ให้ครบ 5 ระดับ |
| Memory Manager / candidate pipeline | — | **NEW** | dedup, scope, confidence, supersede, conflict |
| Reflection engine | — | **NEW** | hook ที่ `done()` / `subagent_done()` |

---

## 5. Platform Layer

| Component | ของเดิม | คำตัดสิน | เหตุผล |
|---|---|---|---|
| Orchestrator | `orchestrator.py` (7,652) | **WRAP — ห้าม rewrite** | เป็น lifecycle controller ตัวเดียวและมี 7,033 tests คุ้มอยู่ · V2 แทรกผ่าน façade เดียวต่อ boundary (assign / done / subagent_done) |
| Scheduler / admission | `resource_governor.py` (654) | **EXTEND** | มี admission + fair queue + resource class แล้ว · เติม dimension: provider slot, account slot, project slot, priority, GPU |
| Process manager | `job_object_manager.py`, PaneState | **EXTEND** | ต้องเพิ่ม track (pid, provider, account, task) ให้ครบตาม §07 |
| Event bus | Qt signals (`statusChanged`, `agentDone`, `ledgerChanged`, …) | **REUSE** | Qt signal คือ event bus ที่มีอยู่แล้วและเข้ากับ UI · **อย่าเพิ่ม bus ตัวที่สอง** — ทำ adapter ชื่อ event ของ V2 → signal เดิมแทน |
| Config store | `config.py` + 14 ไฟล์ json กระจาย | **REPLACE (ค่อยเป็นค่อยไป)** | V2 ต้องแยก config/state/runtime/cache · แต่ทุกไฟล์ต้องมี legacy reader ก่อนย้าย |
| Version manager | `update_*.py`, `release.py` | **EXTEND** | มี app update แล้ว · เพิ่ม schema version + provider detected version |
| Compatibility manager | — | **NEW** | บทเรียน D3 บังคับให้มี |
| Migration engine | ad-hoc (`provider_bootstrap`) | **NEW** | แต่ **ใช้ pattern เดิมเป็นแม่แบบ**: copy-never-move + marker + `os.replace` |
| Secret manager | ไฟล์ credential + Keychain fallback | **NEW** | ต้องมี interface กลางก่อนถึงจะทำ multi-account ได้ปลอดภัย |
| Observability | `EVENTS_LOG` + `_log_event()` + `doctor.py` + `maintenance.py` | **REUSE + EXTEND** | มี structured event log อยู่แล้ว · เติม audit fields (who/agent/provider/account/tool) |
| Doctor | `doctor.py` (2,320) | **EXTEND** | ตรงกับ output ที่ §06 อยากได้อยู่แล้ว แค่เพิ่มบรรทัด schema/adapter/compat |

---

## 6. สรุปเชิงตัวเลข

| คำตัดสิน | จำนวน component | หมายเหตุ |
|---|---|---|
| REUSE | 21 | ส่วนใหญ่คือ execution layer + capability translation ที่ทำมาดีแล้ว |
| WRAP | 6 | orchestrator, spawn engine, transcript readers, plugin installer |
| EXTEND | 14 | ProviderSpec, ResourceGovernor, doctor, digest_facts, lead_context … |
| NEW | 12 | Account/Pool/Selector, Model Profile, Conversation, Checkpoint, Memory Manager, Reflection, Compatibility, Migration, Secret Manager, Capability Gateway |
| REPLACE | 5 | user_profile (เป็น account), effective_provider_for (เป็น router), SKILLS_DIR path, config store layout |

**อ่านง่ายๆ:** ระบบเดิม **แข็งแรงมากในชั้น Execution และ Capability-translation** และ **ว่างเปล่าเกือบสิ้นเชิงในชั้น Intelligence (account/model/router) กับ Cognition (conversation/brain)**
แผน V2 จึงควรเป็น *เติมชั้นที่ว่าง* ไม่ใช่ *รื้อชั้นที่แข็งแรง*

---

## 7. กฎเหล็กของ matrix นี้

1. อะไรที่ตัดสินว่า **REUSE** — ห้ามแก้เพื่อความสวยงามของสถาปัตยกรรม ถ้าไม่มี bug จริง
2. อะไรที่ **WRAP** — โค้ดเดิมต้องยังรันได้เหมือนเดิมเมื่อปิด feature flag
3. อะไรที่ **REPLACE** — ต้องมี legacy reader + migration + validate ก่อนเลิกใช้ ห้ามลบ source
4. **NEW** ทั้งหมดอยู่ใน package ใหม่ที่ import-linter บังคับให้เป็น layer ล่างสุด (ห้าม import UI/orchestrator)
