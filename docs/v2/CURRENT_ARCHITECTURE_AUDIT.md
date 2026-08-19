# V2 Audit — สถาปัตยกรรมปัจจุบันของ agent-takkub

> **Phase 0 deliverable** ของ `agent-takkub-v2-architecture` blueprint (`12_CLAUDE_IMPLEMENTATION_PROMPT.md` → *FIRST TASK: REPOSITORY AUDIT ONLY*)
> **ยังไม่มีการแก้ production behavior ใดๆ** — เอกสารชุดนี้อ่านโค้ดอย่างเดียว

| | |
|---|---|
| Baseline commit | `9c6b56d` — *fix(remote): opencode ตอบกลับไม่ถึงมือถือ… (1.0.75) (#307)* |
| Package version | `1.0.75` |
| วันที่ audit | 2026-08-19 |
| ภาษา/Runtime | **Python 3 + PyQt6** (blueprint เขียนตัวอย่างเป็น TypeScript — ดู §7 R1) |
| ขนาด | 156 modules · 85,099 LOC (`src/agent_takkub/`) |
| Test baseline | **7,033 tests · ผ่านทั้งหมด (exit 0)** · ruff clean · import-linter **25/25 kept** |

---

## 1. Architecture Map ของโค้ดปัจจุบัน

### 1.1 รูปทรงจริง: Qt desktop cockpit ที่ควบคุม CLI ของคนอื่น

agent-takkub **ไม่ได้** เป็น agent runtime ที่คุยกับ LLM API เอง มันเป็น **cockpit** ที่:

1. spawn CLI ของ provider (`claude` / `codex` / `gemini` / `opencode` / `kimi` / `cursor`) เข้าไปใน **PTY** (ConPTY บน Windows, `_pty_backend` บน macOS)
2. render PTY นั้นเป็น **pane** ใน Qt UI
3. ส่ง task เข้าไปโดย **พิมพ์ข้อความ + Enter** (หรือแนบไฟล์ system-prompt ตอน spawn)
4. อ่านผลลัพธ์กลับมาจาก **terminal text + ไฟล์ session ของ provider เอง**
5. รับ "งานเสร็จ" ผ่าน **CLI callback** (`takkub done`) ที่ agent เรียกเอง ผ่าน IPC เข้ามาที่ cockpit

```text
                         Qt MainWindow / ProjectNav
                                    │
                              Orchestrator            ← god object 7,652 LOC
             ┌──────────────┬───────┴───────┬──────────────┐
             │              │               │              │
        SpawnEngine    LeadInbox        LeadWait      AutoResume    (mixins)
             │
        PtySession ──── provider CLI process (claude/codex/…)
             │                    │
        TerminalWidget      ไฟล์ session ของ provider เอง
                                  │
                          remote/notify.py (อ่านกลับมา mirror ขึ้นมือถือ)

  agent ใน pane ── `takkub done` ──→ cli.py ──→ cli_server.py (IPC) ──→ Orchestrator.done()
```

### 1.2 Orchestrator = lifecycle controller ตัวเดียวของระบบ

`orchestrator.py` (7,652 LOC) ประกอบจาก 5 mixins: `PipelineMixin`, `LeadInboxMixin`, `SpawnEngineMixin`, `LeadWaitMixin`, `AutoResumeMixin` + `QObject`
มันคือเจ้าของ pane registry, task identity, การ spawn, การ deliver, การปิดงาน, worktree finalize, shard group, resource token — **ทุกอย่าง**

hook point สำคัญที่ V2 ต้องใช้:

| Boundary | ตำแหน่ง | หมายเหตุ |
|---|---|---|
| assign (จุดร่วมทั้ง 2 mode) | `orchestrator.py:1511 assign()` | `mode` แตกเป็น pane/subagent ที่บรรทัด ~1538 |
| assign → subagent | `orchestrator.py:1300 _register_subagent()` | ไม่มี pane/PTY · เขียน **task capsule** เป็นไฟล์ |
| assign → pane | `orchestrator.py:1744 _assign_dispatch()` | ลำดับประกอบ task ดู §1.3 |
| assign → worktree | `orchestrator.py:2062 _assign_with_worktree()` | ห่อ `_assign_dispatch` |
| completion (pane) | `orchestrator.py:3696 done()` | สร้าง `DigestFacts` |
| completion (subagent) | `orchestrator.py:1436 subagent_done()` | ledger/wait/inbox/worktree sink เดียวกัน |
| measured facts | `orchestrator.py:3504 _compute_digest_facts()` | git state ที่ cockpit วัดเอง ไม่ได้ parse จาก prose |

### 1.3 ลำดับประกอบ task ก่อนส่งเข้า pane (สำคัญมากต่อ Context Builder ของ V2)

```text
raw task
 → _apply_session_goal()                     ← session goal ของ project
 → ps.task_id = uuid4()                      ← task identity เกิดตรงนี้
 → (ถ้า codex) _rewrite_task_for_codex()
 → _append_verify_fail_hint()
 → (ถ้า plan/shard) _wrap_planner_task / _wrap_shard_task
 → _task_handoff_pointer(..., supports_file_read=spec.supports_agent_file_read)
       ├── ถ้ายาว + provider อ่านไฟล์ได้ → เขียนไฟล์ + ส่ง "pointer" สั้นๆ เข้า PTY
       └── ถ้าไม่ → paste ข้อความเต็มเข้า PTY
 → (ถ้า spec.system_prompt_flag) แนบเป็น one-shot system prompt ตอน spawn
 → spawn() → _send_when_ready()
 → task_ledger.create_assignment()
```

**จุดแทรก Context Builder ที่ถูกต้อง** = หลัง `_append_verify_fail_hint` ก่อน `_task_handoff_pointer` — ตรงนั้น delivery/provider-capability logic ทั้งหมดยังทำงานตามเดิม

---

## 2. Subsystem Inventory (ตามรายการที่ blueprint สั่งให้ตรวจ)

### 2.1 Provider integration

| module | LOC | หน้าที่ |
|---|---|---|
| `provider_spec.py` | 1,226 | **`ProviderSpec` dataclass + `PROVIDER_REGISTRY`** — 6 providers, ~40 fields (binary discovery, autonomy flags, ready rules, paste timing, capability flags `supports_*`) |
| `provider_config.py` | 527 | role → provider mapping (`role-providers.json`) + forced roles + override validation |
| `provider_state.py` | 103 | enable/disable ต่อ provider (`disabled-providers.json`) |
| `provider_models.py` | 95 | model ต่อ provider (`provider-models.json`) |
| `role_models.py` | 198 | model+effort ต่อ role **ผูกกับ provider ที่เลือกไว้** (`role-models.json`) |
| `provider_usage.py` | 734 | quota/usage abstraction ข้าม provider (`ProviderUsage`) |
| `provider_bootstrap.py` | 256 | seed prod state เข้า DATA_HOME (1.0.74) |
| `provider_install.py` | 117 | installer ต่อ provider |
| `codex_helper.py` / `gemini_helper.py` / `opencode_helper.py` | 235/560/503 | per-provider store resolution |
| `spawn_engine.py` | 3,065 | argv building + spawn — **claude branch hardcode + generic non-claude branch ที่ขับด้วย ProviderSpec** (`:1690`, `:1723`) |

**ระดับความเป็น adapter วันนี้:** มี *"หนึ่งครึ่ง adapter"* — generic CLI branch หนึ่งตัวที่อ่าน `ProviderSpec` + claude special-case ที่ยัง hardcode
การ branch ตามชื่อ provider ตรงๆ เหลืออยู่ **18 จุดใน 7 ไฟล์** (doctor, cli_server, remote/notify, spawn_engine, usage_meter, roles_page, user_profile)

### 2.2 Account

**ไม่มี concept "Account" แยกจาก Provider** — สิ่งที่ใกล้ที่สุดคือ `user_profile.py` (629 LOC):

- registry `~/.takkub/user-profiles.json` → `{name, config_dir}`
- เลือกต่อ **project** (`projects/<slug>/user-profile.json`)
- inject `CLAUDE_CONFIG_DIR` ตอน spawn

ข้อจำกัด: **claude เท่านั้น**, scope = project (ไม่ใช่ agent/task), ไม่มี pool / selector / priority / quota-aware / cooldown / failover / sticky-session

### 2.3 Model config

`role_models.py` + `provider_models.py` — เป็น **string model id ตรงๆ** ผูกกับ provider
ไม่มี Model Registry (context window, capability, cost class) และไม่มี Model Profile (agent ขอ capability แทนชื่อ model)

### 2.4 Role config (= "Agent" ใน V2)

- built-in roles: `.claude/agents/<role>.md` (`config.AGENTS_DIR` → `ASSETS_ROOT/.claude/agents`)
- custom roles: `custom_roles.py` + `SETTINGS_HOME/agents/`
- `roles.py` (137) — role table · `role_messages.py` (241) — durable send log
- **Role ผูกกับ provider ผ่าน `role-providers.json` แบบ static mapping** ไม่ใช่ routing policy

### 2.5 Pane / Terminal

`pty_session.py` (1,871) · `agent_pane.py` (1,027) · `agent_pane_model.py` (161) · `terminal_widget.py` (780) · `_pty_backend.py` (223, macOS) · `_win_console.py` (105) · `headless_pane.py` / `headless_window.py`
คุณภาพสูง มี ready-marker state machine, paste-swallow recovery, session generation, health tracking — **ส่วนที่ควร reuse มากที่สุดในระบบ**

### 2.6 Worktree

`worktree_manager.py` (1,511) — สร้าง worktree+branch ต่อ pane, baseline SHA snapshot, merge **proposal** (ไม่ auto), safe-remove
มี import-linter contract `worktree-manager-leaf` บังคับให้เป็น leaf module

### 2.7 Project management

`projects.json` (registry) · `project_nav.py` (596) · `project_tab.py` (323) · `project_wizard.py` (620) · `project_rules.py` (113)
`SETTINGS_HOME/projects/<slug>/` เก็บ per-project settings

### 2.8 Runtime / session state

`RUNTIME_DIR = DATA_HOME/runtime/` มี: `sessions/` (decision notes รายวัน), `role-memory/`, `tasks/` (Task Ledger), `messages/`, `progress/`, `agents/` (staged role files), `docs/`, `knowledge/`, `qa-plans/`, `exports/`, `browser-profiles/`, `tunnel/`

**สังเกต:** `runtime/` ปนกันระหว่าง *state ถาวร* (role-memory, tasks, knowledge) กับ *ของชั่วคราวจริงๆ* (tunnel, browser-profiles) และ *ขยะ* (clipboard png 20+ ไฟล์, `events.log.old` 2 MB) — ตรงกับที่ user บอกว่า "มั่วมาก"

### 2.9 Conversation

**ไม่มี Takkub-owned conversation store** — นี่คือช่องว่างใหญ่ที่สุดข้อหนึ่ง:

- ประวัติจริงอยู่ในไฟล์ของ provider เอง (`~/.claude/projects/<enc-cwd>/<uuid>.jsonl`, codex rollout jsonl, agy sqlite + transcript, opencode sqlite)
- `chatlog_scanner.py` (699) + `token_meter.py` (326) + `src/agent_takkub/remote/notify.py` (2,062) **อ่าน** ไฟล์พวกนั้น
- `_save_decision_note` เขียนเฉพาะ **สรุปตอนจบงาน** ไม่ใช่ transcript
- ผลคือ **provider CLI เปลี่ยน schema = ระบบตาบอดเงียบๆ** (พิสูจน์มาแล้วใน 1.0.74 — codex 0.147 + agy store move)

### 2.10 Artifacts / Cache

`DATA_HOME/artifacts/`, `DATA_HOME/cache/`, `graft-graphs/`, `graft-staging/`; `disk_usage.py` (1,356) จัดการพื้นที่
ไม่มี abstraction กลาง — แต่ละ subsystem เขียน path ของตัวเอง

### 2.11 Remote execution

`remote/` (10 modules, ~5,800 LOC) — HTTP server + SSE + bearer auth + Cloudflare tunnel + mobile UI
มี import-linter contract `remote-bolt-on-isolation` (ลบทั้งโฟลเดอร์แล้วระบบยังรันได้)

### 2.12 Issue state

`issues.py` (955) → `gh` CLI · `auto_issue_capture.py` (369) + `auto_issue_signals.py` (325) → auto-file bug จาก signal
state: `.takkub_issues.json`, `auto_issue_dedup.json`

### 2.13 Autoresume

`auto_resume.py` (78, on/off intent) + `limit_autoresume.py` (380, `AutoResumeMixin`) — park pane ตอนชน usage limit, wake เมื่อ window reset
สอง signal ต้องตรงกัน (banner text + usage telemetry) สำหรับ claude

### 2.14 Skill / MCP / Plugin

| module | หน้าที่ |
|---|---|
| `skill_scan.py` (545) | scan `.claude/skills/*/SKILL.md` |
| `skill_policy.py` (244) | role → skill allowlist (`skill-policy.json`) |
| `pane_tools_policy.py` (355) | role → MCP + plugin allowlist (`pane-tools.json`) |
| `mcp_bridge.py` (412) | **แปลง policy เป็น wire format ของแต่ละ provider** — ตัวนี้คือ Capability Hub ตัวอ่อนๆ ที่มีอยู่แล้ว |
| `shared_dev_tools.py` (1,312) | shared MCP server set + browser profile isolation |
| `plugin_installer.py` (474) | curated plugin set ผ่าน `claude plugin` CLI |
| `pane_guard.py` (456) | **shell-side guard** — เพราะ MCP gate อย่างเดียวถูกอ้อมผ่าน Bash ได้ |
| `permission_gates.py` (222) | อ่าน `.claude/settings.json` permission rules |
| `autoskills_installer.py` (864) | ติดตั้ง skill อัตโนมัติ |

**ปัญหาเชิงหลักการ:** คลังกลางอยู่ที่ `ASSETS_ROOT/.claude/skills` และ plugin ติดตั้งผ่าน `claude plugin` CLI — ชื่อและกลไก **ผูกกับ claude** ทั้งที่ตั้งใจให้เป็นของกลาง (user ทักเรื่องนี้เอง: *"คำว่า claude มัน กลาง ตรงไหนวะ"*)

### 2.15 Memory / Second Brain

| ที่มีอยู่ | คือ |
|---|---|
| `role_memory.py` (637) | markdown ต่อ (role × project) + L2 archive — agent เขียนเองอิสระ |
| `bm25_search.py` (222) | BM25 hand-rolled เหนือ session jsonl + role-memory archive |
| `task_ledger.py` (553) | markdown ledger ของทุก assign |
| `vault_mirror.py` (710) + `vault_graph.py` (405) | mirror decision note ไป Obsidian |
| `runtime/knowledge/*.md` | project knowledge เขียนมือ |
| `digest_facts.py` (143) | **facts ที่ cockpit วัดเอง** (git state) แยกจาก prose ของ agent |

**ไม่มี:** Memory Manager, candidate pipeline, scope, confidence, dedup, supersede, conflict detect, retrieval ranking, reflection engine, context budget
`digest_facts.py` คือ *เมล็ดของ provenance model* ที่ V2 ต้องการอยู่แล้ว (measured vs reported)

### 2.16 Scheduler / Resource

`resource_governor.py` (654) — admission + fair queue + resource class (`classify_resource`) + `holders_for_class`
`orchestrator.assign()` ขอ slot จริงก่อน dispatch, มี fan-out queue (flag-gated `TAKKUB_QUEUE_FANOUT`)
`job_object_manager.py` (134) — Windows job object กัน process หลุด
`system_baseline.py` (163) + `performance_settings.py` (163)

**ไม่มี:** provider/account concurrency slot, GPU/VRAM awareness, priority class, backpressure ladder, pause/checkpoint/resume

### 2.17 Version / Migration

`update_helper.py` (420) + `update_worker.py` (358) + `update_panel.py` (1,183) + `claude_update.py` (429) + `release.py` (336)
`doctor.py` (2,320) — 30+ health checks

**ไม่มี:** schema version, compatibility matrix, migration engine, dry-run, journal, rollback, backup manager
ปัจจุบัน migration ทำแบบ ad-hoc ในโค้ด (เช่น `provider_bootstrap.ensure_provider_home` seed ครั้งเดียวด้วย marker file)

### 2.18 Security / Secrets

- credential ของ provider = ไฟล์ของ provider เอง (`.credentials.json` ของ claude, `auth.json` ของ codex) ที่ cockpit **คัดลอก** เข้า DATA_HOME
- macOS: claude เก็บใน Keychain ไม่ใช่ไฟล์
- `src/agent_takkub/remote/auth.py` — bearer token + lockout + SSE ticket
- ไม่มี `SecretManager` abstraction, ไม่มี `secretRef`, ไม่มี audit log กลาง, ไม่มี redaction layer กลาง

---

## 3. Architectural Guardrails ที่มีอยู่แล้ว (ต้องไม่ทำพัง)

1. **import-linter 25 contracts** (enforced ใน CI) — engine/UI separation, CLI-IPC boundary, leaf purity, `provider-spec-pure`, `remote-bolt-on-isolation` ฯลฯ
2. **`docs/architecture/depgraph.json`** — import map auto-refresh ทุก commit (pre-commit hook `depgraph-fresh`)
3. **`docs/architecture/godfile-map.md`** — method→module map ของ god-files
4. **CI matrix `windows-latest` + `macos-latest` ต้องเขียวทั้งคู่**
5. **7,033 tests** เป็น regression net ที่ใหญ่พอจะ refactor ได้จริง
6. **Multi-provider rule** (user directive) — ทุก feature ต้องคิดถึงทุก provider หรือประกาศ gap
7. **Test tier policy** — targeted ระหว่างทาง, full suite ครั้งเดียวที่ qa gate

---

## 4. Storage Layout ปัจจุบัน vs V2 (`02_STORAGE_AND_FOLDER_STRUCTURE.md`)

`DATA_HOME` = `~/.agent-takkub` (installed) หรือ `REPO_ROOT` (dev checkout)
`SETTINGS_HOME` = `DATA_HOME` (installed) หรือ `~/.takkub` (dev)

```text
~/.agent-takkub/                      V2 ที่ blueprint ต้องการ
├── agents/                     →     agents/templates/
├── artifacts/                  →     projects/<id>/artifacts/
├── cache/                      →     cache/
├── claude-config/              →     providers/claude/ + accounts/claude/ + secrets/
├── claude-config.partial/      →     (merge)
├── codex-home/    (1.0.74)     →     providers/codex/ + accounts/codex/
├── opencode-home/ (1.0.74)     →     providers/opencode/ + accounts/opencode/
├── graft-graphs/ graft-staging/ →    plugin storage / cache
├── projects/                   →     projects/
├── project-skills/             →     capabilities/skills/
├── runtime/                    →     runtime/ + state/ + projects/<id>/{tasks,brain,logs}
├── worktrees/                  →     projects/<id>/worktrees/
├── venv/                       →     (คงเดิม — install target)
└── *.json (14 ไฟล์)            →     config/ + state/ + registry ต่างๆ
```

รายละเอียด mapping ไฟล์ต่อไฟล์อยู่ใน `13_MIGRATION_MAPPING_FROM_V1.md` แล้ว และตรงกับของจริงที่ตรวจพบบนเครื่อง

---

## 5. Technical Debt หลัก

| # | หนี้ | หลักฐาน | ผลต่อ V2 |
|---|---|---|---|
| D1 | **god object** `orchestrator.py` 7,652 LOC | `assign()` เดี่ยวๆ ยาว ~230 บรรทัด รวม resource governor + fan-out queue + worktree + provider override ไว้ด้วยกัน | ทุก hook ของ V2 ต้องแทรกในไฟล์นี้ — ความเสี่ยง merge/regress สูง |
| D2 | **ไม่มี conversation store ของตัวเอง** | ประวัติอยู่ในไฟล์ provider; `src/agent_takkub/remote/notify.py` มี adapter อ่าน 4 รูปแบบ | Conversation V2 / Checkpoint / provider switch ทำไม่ได้เลยจนกว่าจะมีตัวนี้ |
| D3 | **provider CLI schema drift ทำระบบตาบอดเงียบ** | 1.0.74: codex 0.147 เปลี่ยน `agent_message`→`item_completed`, agy ย้าย store ทั้งดุ้น; CI เขียวตลอดเพราะ `codex exec` ยังเขียน schema เก่า | ต้องมี Compatibility Manager + probe กับ store จริง ไม่ใช่ mock |
| D4 | **Account ไม่มีจริง** | `user_profile.py` = claude-only, project-scoped | multi-account / failover / quota routing ยังไม่มีฐานรองรับเลย |
| D5 | **Capability store ผูกชื่อ claude** | `SKILLS_DIR = ASSETS_ROOT/.claude/skills`; plugin ผ่าน `claude plugin` CLI | ขัดหลัก "Capability ≠ Provider" ตรงๆ |
| D6 | **`runtime/` ปนกัน 3 ชนิดข้อมูล** | state ถาวร + temp + ขยะอยู่ที่เดียวกัน | Storage V2 migration ต้องแยก config/state/runtime/cache ให้ได้ก่อน |
| D7 | **ไม่มี schema version / migration engine** | ไม่มี `version.json` ระดับ storage; migration = ad-hoc marker file | blueprint บังคับ dry-run + journal + rollback |
| D8 | **main-thread I/O ยังค้าง** | `takkub ma` 2026-08-19: 205 เหตุการณ์ "UI ค้าง (main thread I/O)" ใน 24 ชม. สูงสุด 4.8s | Brain/Context Builder ห้ามเพิ่ม I/O บน Qt tick เด็ดขาด |
| D9 | **Secrets ไม่มี abstraction** | คัดลอกไฟล์ credential ตรงๆ; Keychain มี fallback แต่ไม่ผ่าน interface กลาง | Secret Manager ของ V2 ต้องสร้างใหม่ทั้งชั้น |
| D10 | **Tool execution ไม่ได้ผ่าน cockpit** | provider CLI เป็นคนเรียก tool เอง; cockpit ควบคุมได้แค่ MCP allowlist + shell guard | **Tool Gateway แบบเต็มของ blueprint เป็นไปไม่ได้กับ CLI provider** — ดู §7 R2 |

---

## 6. ช่องว่างเทียบ Acceptance Criteria (`11_ACCEPTANCE_CRITERIA.md`)

| หมวด | มีแล้ว | ขาด |
|---|---|---|
| A. Provider | registry 6 เจ้า, spec-driven spawn, version detect (บางส่วนใน doctor), health check | REST / OpenAI-compatible / Local LLM transport, adapter contract จริง |
| B. Accounts | claude user-profile ต่อ project | ทุกอย่างที่เหลือ (pool, selector, quota, failover, sticky, concurrency, secretRef) |
| C. Model | model id ต่อ role/provider | registry, profile, context window, capability matching |
| D. Capability Hub | policy + `mcp_bridge` แปลงต่อ provider, `pane_guard` | Tool Gateway, permission engine, audit, non-native fallback |
| E. Conversation | — | ทั้งหมด |
| F. Second Brain | role_memory, BM25, vault mirror, digest facts | Manager, scope, confidence, dedup, supersede, retrieval, reflection, context builder |
| G. Version/Migration | update flow, doctor | schema version, compat matrix, dry-run, journal, rollback |
| H. Runtime | ResourceGovernor, job object, fan-out queue | provider/account slot, GPU, priority, backpressure, pause/checkpoint |
| I. Multi-user | — | ทั้งหมด (ระบบเป็น single-user desktop) |
| J. Backward compat | ยังไม่มี migration ให้ต้อง compat | ต้องสร้างพร้อม storage V2 |

---

## 7. สิ่งที่ blueprint สันนิษฐานไม่ตรงกับ repo นี้ + Risk List

ต้องบันทึกไว้ เพราะมีผลต่อแผนโดยตรง:

1. **ภาษา** — blueprint ให้ interface เป็น TypeScript (`interface ProviderAdapter { … Promise<…> }`) แต่ repo เป็น Python/PyQt6 sync-first ไม่มี async event loop ทั่วระบบ → ต้องแปลงเป็น `Protocol` + dataclass และคง sync + QThread pattern เดิม
2. **`.takkub/`** — blueprint พูดถึง root data ว่า `.takkub/` แต่ของจริงคือ `~/.agent-takkub` (prod data) และ `~/.takkub` (settings ของ dev checkout เท่านั้น) — คนละตัวกัน
3. **"Provider Session"** — blueprint คิดว่า cockpit create/resume session ได้ตามใจ แต่ของจริง session เป็นของ CLI และบาง provider **ไม่มี resume เลย** (`spec.supports_resume`)
4. **Tool Gateway** — สมมติว่า tool call ทุกอันวิ่งผ่าน core ได้ แต่ CLI provider เรียก tool เองในกระบวนการของมัน cockpit แทรกกลางไม่ได้
5. **Multi-human-user** — repo เป็น desktop app คนเดียว ไม่มี auth/session ของ human user (มีแค่ bearer token ของ remote)

| id | ความเสี่ยง | ระดับ | การลด |
|---|---|---|---|
| R1 | blueprint เป็น TS/async — แปลงตรงๆ จะได้สถาปัตยกรรมที่ไม่เข้ากับ PyQt6 | สูง | Phase 1 นิยาม `Protocol` แบบ sync + งานหนักอยู่ QThread เหมือนเดิม |
| R2 | **Tool Gateway เต็มรูปแบบทำไม่ได้กับ CLI provider** | สูง | ลด scope เป็น *Capability Gateway*: คุม allowlist/permission/audit ที่ขอบ (MCP config + `pane_guard` + hook) ไม่ใช่ intercept ทุก tool call · ประกาศเป็น gap ชัดๆ |
| R3 | แตะ `orchestrator.assign/done` = แตะหัวใจระบบ | สูง | แทรกผ่าน façade เดียว + fail-open + feature flag; targeted tests ทุกครั้ง |
| R4 | Storage migration ทำ user เสียงาน/เสีย login | สูงมาก | copy-never-move, backup, dry-run, journal, ห้ามลบ source จนกว่า validate ผ่าน (มี precedent ใน `provider_bootstrap`) |
| R5 | เพิ่ม I/O บน Qt main thread → UI ค้าง (D8 พิสูจน์แล้ว) | สูง | materialize current-state ตอน write, hot read ต้อง bounded, ห้าม walk tree ใน tick |
| R6 | provider CLI drift อีกรอบระหว่างทำ V2 | กลาง | Compatibility Manager + ทดสอบกับ store จริง (บทเรียน 1.0.74) |
| R7 | scope creep — blueprint 10 phase ใหญ่กว่าที่ทีมเดียวจะทำจบ | สูง | ตัดเป็น slice ที่ปล่อยได้จริงต่อ release, ทุก phase ต้อง runnable |
| R8 | import-linter 25 contracts อาจบล็อกโครง `core/` ใหม่ | กลาง | เพิ่ม contract ให้ `core/` เป็น layer ล่างสุดตั้งแต่ Phase 1 |
| R9 | ทำ V2 ไปพร้อม bug fix ปกติ → conflict | กลาง | V2 อยู่ใน package ใหม่แบบ parallel ไม่แก้ของเดิมจนกว่าจะ wrap เสร็จ |
| R10 | ไม่มี multi-user จริงแต่ blueprint บังคับ scope | ต่ำ | ใส่ field `user_id/workspace_id` ใน schema ตั้งแต่ต้น แต่ implement single-user ก่อน |

---

## 8. Baseline Test Record

ตามที่ blueprint สั่ง (*record failing tests before implementation*):

```text
pytest -q              → EXIT 0 · 7,033 tests collected · ไม่มี failure
ruff check src/ tests/ → All checks passed
lint-imports           → Contracts: 25 kept, 0 broken
```

**ไม่มี failing test ค้างอยู่ก่อนเริ่ม V2** — regression ใดๆ หลังจากนี้เป็นของ V2 ล้วน

---

*เอกสารคู่กัน:* [`REUSE_VS_REWRITE_MATRIX.md`](REUSE_VS_REWRITE_MATRIX.md) · [`V2_IMPLEMENTATION_PLAN.md`](V2_IMPLEMENTATION_PLAN.md)
