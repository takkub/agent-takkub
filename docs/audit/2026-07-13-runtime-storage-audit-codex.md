# Runtime/storage audit — agent-takkub

วันที่ตรวจ: 2026-07-13  
ขอบเขต: mutable state, task handoff/ledger, pane transcripts, screenshots/images, generated docs, worktrees, provider-owned sessions, project-local files และ retention

## สรุปคำตอบ

คำตอบคือ **ไม่ได้เก็บทุกอย่างไว้ใน `.agent-takkub` และไม่ได้เก็บทุกอย่างไว้ใน repo แบบเดียวกัน** การวางไฟล์ขึ้นกับ mode และชนิดข้อมูล:

1. **instance ที่กำลังรันอยู่ตอนนี้เป็น dev/source checkout** ดังนั้น `DATA_HOME == REPO_ROOT` และ runtime จริงอยู่ที่
   `C:\Users\monch\WebstormProjects\agent-takkub\runtime` ไม่ใช่ `C:\Users\monch\.agent-takkub\runtime`
2. **installed desktop build ปกติ** ใช้ `~/.agent-takkub` เป็น `DATA_HOME`; runtime, projects registry, worktrees และ central custom skills จึงไปอยู่ใต้ home นี้
3. **ไฟล์งานจริง** เช่น source code, tests, configs และ deliverable ที่ policy ระบุว่าเป็น project-owned ยังคงเขียนลง active project repo หรือ isolated git worktree ตามปกติ
4. **รูปชั่วคราว/screenshots/test scripts** ถูกชี้ด้วย `TAKKUB_ARTIFACTS_DIR` ไปที่ `DATA_HOME/runtime/exports/<date>/<project>`
5. **generated docs เป็นแบบผสม**: design reviews, guides และ system overview มี central path `TAKKUB_DOCS_DIR`; แต่ analyst specs, security findings และกติกา AGENTS.md สำหรับ review/analysis ยังสั่งเขียน `docs/` ใน project repo
6. **provider มี store ของตัวเองอีกชั้น**: Claude session JSONL อยู่ใน Claude config dir, Codex เก็บของมันใต้ `~/.codex`, Gemini/Agy ใช้ `~/.gemini`; ไม่ได้ถูกย้ายเข้า agent-takkub ทั้งหมด
7. มี optional Obsidian mirror นอกทั้ง repo และ `.agent-takkub` ถ้าพบ vault ที่กำหนดไว้

ดังนั้น ถ้าเป้าหมายคือ “ทุกไฟล์ที่ agent-takkub สร้างเองต้องไม่ทำให้ project repo สกปรก และต้องรวมใต้ `.agent-takkub`” ระบบปัจจุบัน **ยังไม่บรรลุเป้าหมายนั้นเต็มร้อย**

## หลักฐานจาก instance ปัจจุบัน

ค่า resolved จาก `agent_takkub.config` ใน pane นี้:

```text
AGENT_TAKKUB_HOME env=None
REPO_ROOT=C:\Users\monch\WebstormProjects\agent-takkub
DATA_HOME=C:\Users\monch\WebstormProjects\agent-takkub
SETTINGS_HOME=C:\Users\monch\.takkub
RUNTIME_DIR=C:\Users\monch\WebstormProjects\agent-takkub\runtime
PROJECTS_JSON=C:\Users\monch\WebstormProjects\agent-takkub\projects.json
DOCS_DIR=C:\Users\monch\WebstormProjects\agent-takkub\runtime\docs
PROJECT_SKILLS_HOME=C:\Users\monch\WebstormProjects\agent-takkub\project-skills
default_claude_config_dir=C:\Users\monch\.claude
```

ค่า env ที่ cockpit stamp ให้ pane นี้:

```text
TAKKUB_PROJECT=agent-takkub
TAKKUB_ROLE=lead
TAKKUB_ARTIFACTS_DIR=C:\Users\monch\WebstormProjects\agent-takkub\runtime\exports\2026-07-13\agent-takkub
TAKKUB_DOCS_DIR=C:\Users\monch\WebstormProjects\agent-takkub\runtime\docs\agent-takkub
```

สถานะพื้นที่ปัจจุบันจากการอ่าน filesystem:

| Path | จำนวนไฟล์ | ขนาดโดยประมาณ |
|---|---:|---:|
| `runtime/` ทั้งหมด | 24,758 | 1,727.22 MiB |
| `runtime/sessions/` | 3,880 | 450.72 MiB |
| `runtime/browser-profiles/` | 2,069 | 193.55 MiB |
| `runtime/exports/` | 927 | 79.51 MiB |
| `runtime/tasks/` | 846 | 2.93 MiB |
| `runtime/docs/` | 0 | 0 MiB |

`runtime/`, `projects.json` และ `worktrees/` ถูก `.gitignore` ใน repo นี้ จึงอยู่ “ภายในโฟลเดอร์ repo” แต่ไม่ควรถูก commit (`.gitignore:15-17,65-67`) อย่างไรก็ตามการอยู่ใน repo checkout ยังมีผลด้านขนาด backup, IDE indexing, antivirus และการลบ/ย้าย checkout

## กติกาเลือก DATA_HOME

Source: `src/agent_takkub/config.py:108-157`

ลำดับ resolve คือ:

1. ถ้ามี `AGENT_TAKKUB_HOME` ใช้ path นั้นตรง ๆ
2. ถ้าเป็น source checkout ที่มี `pyproject.toml` + `src/` ใช้ repo root
3. ถ้า package อยู่ใต้ venv ที่ติดตั้ง ใช้ parent ของ venv
4. fallback เป็น `~/.agent-takkub`

ผลของแต่ละ mode:

| Mode | DATA_HOME | SETTINGS_HOME | Claude default config |
|---|---|---|---|
| dev/source checkout ปัจจุบัน | repo `agent-takkub` | `~/.takkub` | `~/.claude` |
| installed desktop | `~/.agent-takkub` หรือ home ของ isolated venv | เท่ากับ DATA_HOME | `DATA_HOME/claude-config` |
| explicit override | `$AGENT_TAKKUB_HOME` | เท่ากับ DATA_HOME | `DATA_HOME/claude-config` |
| Docker | `/data` named volume | `/data` | `/data/claude-config` |

Docker ยืนยันใน `docker-compose.yml:5-30` และ `Dockerfile:38-45` ว่าใช้ `AGENT_TAKKUB_HOME=/data` พร้อม named volume

## Persistence map

### 1. DATA_HOME / runtime-owned

Source constants: `src/agent_takkub/config.py:281-349`

| ข้อมูล | Path ภายใต้ DATA_HOME | หมายเหตุ |
|---|---|---|
| project registry | `projects.json` | active project, paths, presets |
| port/audit | `runtime/port`, `runtime/events.log` | events log rotatesเป็น `.old` เมื่อเกิน limit |
| role prompt staging | `runtime/agents/<role>/CLAUDE.md` | mutable staging ไม่ใช่ app-shipped role source |
| full task handoff + ledger | `runtime/tasks/<project>/<date>/...`, `INDEX.md`, `.ledger-state.json` | ทุก assign มี ledger; task ยาวเกิน 400 chars มี pointer file |
| pane raw PTY transcript | `runtime/sessions/<date>/<project>/<role>-<time>.transcript.log` | opt-out ได้ด้วย `TAKKUB_DISABLE_TRANSCRIPTS=1` |
| done/lead markdown notes | `runtime/sessions/<date>/<project>/<role>-<time>.md` | อยู่ข้าง transcript |
| temp/screenshots/evidence | `runtime/exports/<date>/<project>/` | path เดียวกันทุก pane ของ project ในวันเดียวกัน |
| central generated docs | `runtime/docs/<project>/` | ใช้ผ่าน `TAKKUB_DOCS_DIR` |
| browser profiles | `runtime/browser-profiles/<project>-<role>...` | persistent เพื่อเก็บ login/cookies |
| Codex crash diagnostics | `runtime/codex_crash_dumps/` | exit code, PTY tail และ filtered env keys |
| QA plan fanout | `runtime/qa-plans/` | planner JSON |
| role memory | `runtime/role-memory/` | learned role notes |
| shared MCP snapshots | `runtime/shared-mcp*.json` | generated per role/project variants |
| webengine cache/state | `runtime/webengine/` | cockpit Chromium storage/cache |
| tunnel state | `runtime/tunnel/` | remote tunnel runtime |
| central custom skills | `project-skills/<project>/<skill>/SKILL.md` | นอก runtime แต่ยังใต้ DATA_HOME |
| managed worktrees | `worktrees/<project>/<role>-<timestamp>` | checkout เต็มของ repo สำหรับ isolation |

Task handoff path อยู่ที่ `src/agent_takkub/orchestrator_text.py:422-469`; ledger อยู่ที่ `src/agent_takkub/task_ledger.py:1-22,63-75`; transcript path อยู่ที่ `src/agent_takkub/orchestrator_text.py:743-765`; worktree root อยู่ที่ `src/agent_takkub/worktree_manager.py:267-283`

### 2. Settings store

ใน dev ปัจจุบัน settings อยู่ `~/.takkub`; installed build รวมไว้ใน `DATA_HOME`:

- `role-providers.json`, `disabled-providers.json`
- `pipelines.json` และ `projects/<slug>/pipelines.json`
- `user-profiles.json`, `projects/<slug>/user-profile.json`
- `custom-roles.json`, `agents/<role>.md`
- `pane-tools.json`, `skill-policy.json`
- `exec-mode.json`, `autoresume.json`, `plan.json`, `remote.json`, `rtk-enabled.json`
- auth override config (ไม่ใช่ provider credentials หลัก)

หลักฐานรวม: `src/agent_takkub/config.py:141-157,281-313` และ modules ที่ประกาศ `_PATH = SETTINGS_HOME / ...`

### 3. Active project repo / worktree

ไฟล์ต่อไปนี้ยังลง active project โดยตั้งใจหรือโดย design:

- source code, tests, configs และ implementation ทุกอย่างที่ task ขอให้แก้
- หาก `--isolation worktree` จะลง checkout ใต้ `DATA_HOME/worktrees/...`; หากไม่ใช้ isolation จะเขียน shared cwd โดยตรง
- project rules `CLAUDE.md` ที่สร้างจาก wizard (`src/agent_takkub/project_rules.py:103-120`)
- cockpit-managed `AGENTS.md` สำหรับ Codex/Gemini ถูก plant ที่ spawn cwd; เพิ่ม `AGENTS.md` ใน `.git/info/exclude` เพื่อไม่ให้แสดงใน git status แต่ไฟล์ยังอยู่ใน project filesystem (`src/agent_takkub/codex_agents_md.py:147-188,191-241`)
- optional `.takkub/worktree.json` เป็น project config สำหรับ symlink/postCreate/base_port
- local issue fallback `.takkub_issues.json` ลง current cwd เมื่อ GitHub/gh ใช้ไม่ได้ (repo นี้ ignore ไว้ แต่ project อื่นขึ้นกับ `.gitignore` ของ project นั้น)
- project-side `.claude/skills/<name>` อาจเป็น junction/symlink ชี้กลับ central custom skill; real content อยู่ central แต่ link point ยังอยู่ใน project tree

### 4. Provider-owned stores

- Claude Code session JSONL: dev ใช้ `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`; installed default ใช้ `DATA_HOME/claude-config/projects/...` (`src/agent_takkub/chatlog_scanner.py:1-46`)
- Codex: cockpit wrapper ไม่เขียนเอง แต่ Codex เก็บ session artifacts ใต้ `~/.codex/` (`src/agent_takkub/codex_helper.py:17-21`)
- Gemini/Agy: มี machine-wide registry ใต้ `~/.gemini/config/plugins/...` สำหรับ plugin import; agent-takkub หลีกเลี่ยง auto-mutating registry นี้ (`src/agent_takkub/provider_spec.py:294-307`)
- provider credentials/history จึงไม่ใช่ข้อมูลที่รวมใต้ `.agent-takkub` ทั้งหมด

### 5. OS/global/optional stores

- Windows `QSettings("agent-takkub", "cockpit")`: geometry, splitter, per-role font; โดยปกติอยู่ Registry
- temp dir: instance lock และ multi-instance port file (`agent-takkub-cockpit-*.lock`, `agent-takkub-port.<pid>`)
- optional vault: `$TAKKUB_VAULT_DIR` หรือ `~/second-brain` ถ้ามี `01-Projects/`; mirrors done/session notes ไป `99-Logs/sessions`, briefs, daily note และ `hot.md`
- เครื่องนี้ ณ ตอนตรวจไม่มี `TAKKUB_VAULT_DIR` และไม่มี default `~/second-brain/01-Projects`, ดังนั้น mirror ไม่ active

## รูป/ภาพ ถูกเก็บอย่างไร

### Screenshot/evidence ที่ agent สร้าง

ทุก provider branch เรียก `_apply_artifacts_dir`; env ชี้ไป:

```text
DATA_HOME/runtime/exports/<YYYY-MM-DD>/<project>/
```

QA convention เพิ่ม `screenshots/<view>.png`; role อื่นถูกแนะนำให้ใช้ subdir ของ role เพื่อไม่ให้ evidence scan หยิบข้าม pane (`src/agent_takkub/pane_env.py:250-285`, `.claude/agents/qa.md:115-130`)

ข้อจำกัดสำคัญ:

- นี่คือ **environment + prompt convention ไม่ใช่ write sandbox**; agent/tool ยังเขียนที่อื่นได้ถ้า task/path/default ของ tool สั่งเช่นนั้น
- path แยกแค่ date+project ไม่แยก task/session/role โดยระบบ จึงมีโอกาสชนชื่อ/overwrite หรืออ่านภาพข้าม pane; role subdir เป็นเพียงคำแนะนำ
- evidence scanner ใช้ mtime หลัง assign ช่วยกรองของเก่า แต่ไม่ป้องกัน overwrite

### รูปที่ paste ผ่าน Ctrl+V ใน terminal

Cockpit decode แล้วเขียนตรงที่ `RUNTIME_DIR/clipboard-<timestamp>.png`; เก็บ 50 ไฟล์ล่าสุดและลบเก่าสุดเมื่อมี paste ใหม่ (`src/agent_takkub/terminal_widget.py:43,61-93,713-738`)

### รูปที่ drag/drop

Cockpit ไม่ copy ไฟล์ เพียง insert absolute path เดิมเข้า terminal ดังนั้น original image อยู่ที่ตำแหน่งเดิมของผู้ใช้ (`src/agent_takkub/terminal_widget.py:56-58,670-703`)

## Generated docs ถูกเก็บอย่างไร

มีสอง policy พร้อมกัน:

### Central docs (นอก active project repo)

`TAKKUB_DOCS_DIR = DATA_HOME/runtime/docs/<project>` สำหรับ:

- `design-review/`
- `guides/`
- `system-overview/`

ดู `CLAUDE.md:245-275`, `.claude/agents/critic.md:95-161` และ `.claude/agents/docs.md:31-56`

### Project-owned docs (อยู่ใน repo/worktree)

- analyst: `docs/specs/...` (`.claude/agents/analyst.md:31-44`)
- security: `docs/security/...` (`.claude/agents/security.md:31-58`)
- designer handoff examples: `docs/design/...`
- cockpit-managed Codex/Gemini `AGENTS.md` สั่ง review/analysis/planning ทุกชนิดให้ save markdown ใต้ `docs/` ใน cwd ก่อน `takkub done` (`src/agent_takkub/codex_agents_md.py:48-57`)
- task prompt ระบุ path ใน repo โดยตรงย่อมชนะ central convention

นี่เป็น **policy inconsistency ที่อธิบายอาการ “doc ยังไปโผล่ใน repo”** โดยตรง โดยเฉพาะงาน analysis ผ่าน Codex/Gemini แม้ `TAKKUB_DOCS_DIR` จะถูก stamp แล้วก็ตาม ใน session นี้เอง AGENTS.md บังคับให้ audit นี้อยู่ใต้ `docs/` จึงเป็นตัวอย่างของ exception นี้

## Retention / cleanup

ที่มี implementation ชัดเจน:

- raw PTY `*.transcript.log`: prune ตอน cockpit boot เมื่อเก่ากว่า 7 วัน; markdown done notes ไม่ถูกลบด้วย routine นี้ (`src/agent_takkub/orchestrator_text.py:51,205-232`)
- browser profiles: prune ตอน boot เมื่อ mtime เก่ากว่า 14 วัน (`src/agent_takkub/shared_dev_tools.py:327-377`)
- clipboard images: เก็บ 50 ล่าสุด โดย cleanup ทำเมื่อ paste รูปใหม่
- `events.log`: rotate เป็น `events.log.old` เมื่อเกิน size cap
- vault mirror: มี retention แยกเมื่อ vault active
- worktree: done/close พยายาม propose merge หรือ safe-remove ตามสถานะ commit

ที่ไม่พบ general retention/cleanup ครอบคลุมใน code path หลัก:

- `runtime/exports/`
- task ledger/detail/pointer files ใต้ `runtime/tasks/`
- central docs ใต้ `runtime/docs/`
- markdown session summaries ใต้ `runtime/sessions/`
- crash dumps และ miscellaneous runtime files บางชนิด

ตัวเลข 1.73 GiB ปัจจุบันยืนยันว่าการสะสมยังมีนัยสำคัญ แม้บางหมวดมี age prune แล้ว ไม่ควรลบอัตโนมัติโดยไม่มี preview เพราะ sessions, tasks และ browser profilesอาจมีข้อมูลที่ผู้ใช้ยังต้องการ

## Findings / risks

### P1 — ตำแหน่ง dev กับ installed ต่างกันมากและ UI/เอกสารทำให้เข้าใจว่า “ทุกอย่างอยู่ ~/.agent-takkub”

README พูดถึง isolated `~/.agent-takkub` สำหรับ installation แต่ source checkout ตั้งใจใช้ repo root ทำให้ developer เห็น runtime 1.7 GiB ภายใน checkout แม้ Git ignore อยู่ ความต่างนี้ควรแสดงใน Settings/About/Doctor อย่างชัดเจนด้วย resolved `DATA_HOME`

### P1 — docs routing ไม่เป็น single policy

มีทั้ง central docs และ repo docs; AGENTS.md generic review rule ขัดกับ central-home design สำหรับ reviews/system docs ผลคือ provider/role และ wording ของ task เปลี่ยนปลายทาง ผู้ใช้คาดเดาไม่ได้จากชนิดไฟล์อย่างเดียว

### P1 — artifacts path เป็น convention ไม่ใช่ enforcement

การ stamp env ไม่สามารถรับประกันว่า image/doc/temp output ทุก tool จะไป central store จำเป็นต้องมี post-task repo-junk detector หรือ filesystem policy ถ้าต้องการ guarantee

### P2 — runtime cleanup ยังไม่ครอบคลุมและไม่มี user-facing inspect/clean flow

current runtime 24k+ files / 1.7 GiB; exports, task files และ markdown summaries ไม่มี age/cap policy ทั่วไป

### P2 — runtime และ docs อาจมีข้อมูลละเอียดอ่อน

PTY transcripts เก็บ raw output และ task files เก็บ full prompt; screenshot/clipboard images อาจมีข้อมูลผู้ใช้ แม้ runtime ถูก Git-ignore ก็ยัง durable บน disk และอาจเข้า backup/AV/indexer ควรมี retention/privacy UI และอธิบาย `TAKKUB_DISABLE_TRANSCRIPTS`

### P2 — date+project artifacts namespace shared ข้าม panes/tasks

ชื่อไฟล์ชนกันได้; mtime filtering แก้ stale pickup แต่ไม่แก้ collision ควรเพิ่ม task/session/role namespace โดยคง compatibility alias สำหรับ `screenshots/`

## แนวทางปรับให้ตรงเป้าหมาย “central by default”

1. กำหนด policy ให้ชัด:
   - project-owned: source/tests/config/explicit product docs เท่านั้น
   - takkub-owned: prompts, logs, task specs, screenshots, drafts, generated audit/review docs ไป DATA_HOME
2. ทำ dev mode ให้ opt-in/setting เลือก `AGENT_TAKKUB_HOME=~/.agent-takkub-dev` ได้ง่าย พร้อม migration preview; ห้ามย้าย runtime 1.7 GiB เงียบ ๆ
3. เปลี่ยน generic AGENTS.md review rule จาก `docs/` เป็น `$TAKKUB_DOCS_DIR/<category>/` เว้นแต่ task ระบุว่า deliverable ต้อง commit เข้า repo
4. ทำ `takkub storage show` แสดง resolved homes, size per category, retention และ current pane env
5. ทำ `takkub storage clean --dry-run` ก่อนเสมอ; แยก policy sessions/exports/tasks/browser/clipboard และปกป้องไฟล์ที่ถูกอ้างใน ledger/vault
6. เพิ่ม post-task warning เมื่อมี untracked/generated image, log, dump, temp script หรือ generic review markdown เกิดใน project repo โดยไม่อยู่ allowlist
7. เปลี่ยน artifacts namespace เป็น `exports/<date>/<project>/<task-or-session>/<role>/`; expose shared screenshot handoff dir แยกต่างหาก
8. แสดง badge ใน UI ว่า instance เป็น `dev · DATA_HOME=<repo>` หรือ `installed · DATA_HOME=~/.agent-takkub`

## Verdict

- **คำถาม “ตอนนี้เก็บใน project repo หรือ `.agent-takkub`?” — สำหรับ pane/instance นี้ runtime เก็บใน repo `agent-takkub` เพราะรัน dev checkout แต่ Git-ignore; ไม่ได้ใช้ `.agent-takkub`.**
- **สำหรับ installed build runtime ส่วนหลักเก็บใน `.agent-takkub`.**
- **active project repo ยังรับ code changes และ project-owned docs; generated docs policy ปัจจุบันเป็นแบบผสม.**
- **provider histories/configs และ optional vault อยู่คนละ storage domain.**
- จึงไม่ควรอธิบายระบบว่า “ทั้งหมดอยู่ `.agent-takkub`” จนกว่าจะ unify policy และ enforce/migrate จริง
