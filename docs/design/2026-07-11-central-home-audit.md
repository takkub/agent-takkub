---
date: 2026-07-11
author: maintainer
status: audit (ห้ามแก้โค้ด — เอกสารเดียว)
topic: ย้ายทุกอย่างที่ cockpit สร้าง/ใช้ ออกจาก project folder จริง → central home
---

# Central Home Audit — เอาของ cockpit ออกจาก repo จริงของ user

## TL;DR (อ่านแค่นี้พอ)

**ข่าวดี:** cockpit เขียนของ **เกือบทั้งหมด** ลง `DATA_HOME/runtime` (central) อยู่แล้ว —
task ledger, exports/screenshots, role-memory, worktrees, transcripts, port, events, custom-roles
**ไม่ได้แตะ repo จริงของ user เลย**

**สิ่งที่ยัง "รก repo จริง" มีแค่ 3 จุด** และแบ่งเป็น 2 ชนิด:

| # | ไฟล์ที่ไปโผล่ใน project | ใครเขียน | discovery-bound? | ย้ายง่ายแค่ไหน |
|---|---|---|---|---|
| A1 | `<project>/.claude/skills/<name>/SKILL.md` | **cockpit code** (New Skill ปุ่ม) | ✅ claude ต้องเห็นใน cwd | ต้อง junction |
| A2 | `<project>/AGENTS.md` | **cockpit code** (codex/agy spawn) | ✅ codex/agy walk-up จาก cwd | ย้ายไม่ได้ตรงๆ — gitignore แทน |
| A3 | `<project>/.claude/settings.json` (rtk hook) | **cockpit code** (rtk install, user-trigger) | ✅ claude `project,local` | ย้ายเข้า `--settings` inject ได้ |
| C* | `<project>/docs/design-review·reviews·guides·system-overview/*.md` | **agent (LLM)** ตาม CLAUDE.md | ❌ | เปลี่ยน wording — **ต้องถาม user ก่อน** |
| C* | `<project>/runtime/exports/...` (screenshot หลง) | **agent** ตาม example ผิดใน CLAUDE.md | ❌ | แก้ wording ให้ใช้ `$TAKKUB_ARTIFACTS_DIR` |

**คำแนะนำเรื่องชื่อ folder กลาง:** **ใช้ `~/.agent-takkub` ที่มีอยู่แล้ว** (คือ `DATA_HOME` ปัจจุบันในโหมด installed) —
อย่าสร้าง root ที่ 3. `~/.takkub` เป็น legacy (settings-only, dev เท่านั้น) เก็บไว้เพื่อ backwards-compat พอ
รายละเอียด + เหตุผล ดูส่วนที่ 3

**⚠️ ขอบเขตที่เข้าใจให้ตรงกันก่อน:** "project folder จริง" = repo เป้าหมายของ user (เช่น web/api)
ที่ pane รันเป็น cwd — **ไม่ใช่** repo ของ cockpit เอง (agent-takkub) ในโหมด dev
`DATA_HOME/runtime` ในโหมด dev = อยู่ใน repo cockpit เอง ซึ่ง **ไม่ใช่ปัญหา** (เป็น repo ของ maintainer ไม่ใช่ของ user)

---

## 1. ตอนนี้ cockpit เขียนอะไรลง project folder จริงบ้าง

แบ่ง 3 กลุ่มตามว่า "ใครเขียน" และ "ไปที่ไหน" — สำคัญมากเพราะ **วิธีแก้ต่างกันสิ้นเชิง**

### กลุ่ม A — cockpit **code** เขียนลง pane cwd (= repo จริงของ user) ← ต้นเหตุความรก

| ไฟล์ | โมดูล (file:line) | trigger | หมายเหตุ |
|---|---|---|---|
| `<cwd>/.claude/skills/<name>/SKILL.md` | `skill_scan.create_skill` :129–156 · เรียกจาก `settings_window._on_create_skill_clicked` :2159 (root = `_writable_skill_roots()` :2075 = project paths) | user กดปุ่ม **New Skill** ใน Settings → Skill Catalog (commit `50a9984` วันนี้) | เขียน SKILL.md ลง `.claude/skills/` ของ **project** — claude อ่านจาก cwd |
| `<cwd>/AGENTS.md` | `codex_agents_md.ensure_agents_md` :188 · เรียกจาก `spawn_engine` :1124 (gemini/agy) และ :1181 (codex) | ทุกครั้งที่ spawn pane role codex หรือ gemini | marker-guarded (`<!-- takkub-managed AGENTS.md · do not commit -->`) — ถ้า user มี AGENTS.md เองจะไม่แตะ · เขียน/refresh ทุก spawn |
| `<cwd>/.claude/settings.json` (+ สร้าง `.claude/`) | `rtk_helper.install_rtk` :125–126 | user เลือกติดตั้ง rtk hook (opt-in) | idempotent merge PreToolUse Bash hook ลง settings.json ของ project |

> **หมายเหตุ contrast ที่น่าสนใจ:** **custom-role** เขียนลง `SETTINGS_HOME/agents/<role>.md`
> (`config.CUSTOM_AGENTS_DIR` :258) = **central แล้ว** แต่ **custom-skill** ดันเขียนลง project
> → pattern ไม่ตรงกัน (roles central / skills ไม่ central) นี่คือ inconsistency ที่ควรแก้ให้เป็น pattern เดียว

### กลุ่ม B — cockpit เขียน แต่ **central อยู่แล้ว** (ไม่รก repo จริง — list ไว้เพื่อความครบ)

| ของ | path | โมดูล (file:line) |
|---|---|---|
| Task Ledger (INDEX.md + `<hhmmss>-<role>-ledger.md` + `.ledger-state.json`) | `RUNTIME_DIR/tasks/<project>/` | `task_ledger._ledger_dir` :63 |
| Task handoff pointer (dodge paste-swallow) | `RUNTIME_DIR/tasks/<project>/<date>/` | `orchestrator_text._task_handoff_dir` |
| Screenshots / exports / scratch | `RUNTIME_DIR/exports/<date>/<project>/` (env `TAKKUB_ARTIFACTS_DIR`) | `pane_env._apply_artifacts_dir` :258 |
| Per-role learned memory | `RUNTIME_DIR/role-memory/<project>/<role>.md` | `role_memory.ROLE_MEMORY_DIR` :29 |
| Isolated git worktrees (#81) | `DATA_HOME/worktrees/<project>/` | `worktree_manager.worktree_root` :266 |
| PTY transcript, port, events.log | `RUNTIME_DIR/` | `config` :260–261 · `_build_transcript_path` |
| Custom roles | `SETTINGS_HOME/agents/<role>.md` | `config.CUSTOM_AGENTS_DIR` :258 |
| projects.json | DATA_HOME/projects.json (path แปรตามโหมด dev/installed) | `config.PROJECTS_JSON` :244 |
| exec-mode / pane-tools / disabled-providers / user-profiles / plan | `SETTINGS_HOME/*.json` | หลายที่ |
| Lead per-project MEMORY | `~/.claude/projects/<encoded-cwd>/memory/MEMORY.md` | `orchestrator_text._resolve_project_memory` :733 (Claude Code เขียนเอง ไม่ใช่ cockpit) |

> **ประเด็น dev vs installed:** `RUNTIME_DIR = DATA_HOME/runtime`. โหมด dev `DATA_HOME == REPO_ROOT`
> → runtime อยู่ในrepo cockpit เอง (agent-takkub) ✓ ไม่ใช่ repo ของ user. โหมด installed `DATA_HOME == ~/.agent-takkub`
> → runtime อยู่นอก repo ทั้งหมด. **ทั้ง 2 โหมดไม่แตะ repo เป้าหมายของ user เลย** (`config._resolve_data_home` :96)

### กลุ่ม C — **agent (LLM)** เขียนลง cwd ตาม instruction (ไม่ใช่ cockpit code — แก้ที่ wording)

| ของ | เขียนโดย role | มาจาก instruction ที่ไหน |
|---|---|---|
| `docs/design-review/<date>-<view>.md` (+ `.html` sibling จาก `design_review_html.render` :159) | critic | CLAUDE.md pipeline (Design Critic) |
| `docs/reviews/*.md` | reviewer | task spec / feedback_agent_done_protocol |
| `docs/system-overview/<date>-<project>.md` + `.html` | Lead (EXPLAIN_SYSTEM) | CLAUDE.md routing |
| `docs/guides/<date>-<topic>.md` + `.html` | Lead (GENERATE_GUIDE) | CLAUDE.md routing |
| screenshots ที่ **หลงมาอยู่** `<project>/runtime/exports/...` | qa/critic | ⚠️ **บั๊ก wording** — CLAUDE.md pipeline เขียน `save shots to runtime/exports/$(date +%F)/...` เป็น path **relative** → ถ้า agent รันจาก cwd=project มันสร้าง `<project>/runtime/exports/` ในrepo! ทั้งที่ env `TAKKUB_ARTIFACTS_DIR` ชี้ central อยู่แล้ว |

> **หลักฐาน design_review_html:** `render()` :159 เขียน `.html` เป็น sibling ของ `.md`
> (`md_path.with_suffix(".html")`) — ฉะนั้น md อยู่ที่ไหน html ไปที่นั่น. ถ้า md อยู่ project docs/ → html รกด้วย

---

## 2. Constraint การ discover ของแต่ละ CLI (อะไรต้องอยู่ใน cwd เท่านั้น)

หลักฐานจาก `spawn_engine._spawn_claude` :1517–1663 + `codex_agents_md` docstring + `provider_spec`

### claude pane (Lead + teammate ส่วนใหญ่)
อ่านจาก **cwd (+ walk-up)**:
- **`.claude/skills/`** — claude auto-discover skill จาก cwd แบบ native (นี่คือเหตุผลที่ New Skill ต้องเขียนลง cwd)
- **`CLAUDE.md`** — project memory, claude อ่าน+walk up เอง (**user เป็นเจ้าของ — cockpit ไม่ได้เขียนให้**)
- **`.claude/settings.json` + `.claude/settings.local.json`** — เพราะ spawn ด้วย `--setting-sources project,local` (:1526)

**สำคัญ — `--setting-sources project,local` แปลว่า:**
- claude **ไม่อ่าน** `~/.claude/settings.json` (user layer ถูกตัดออก) — จงใจเลี่ยง claude-obsidian SessionStart hook ที่ crash
- สิ่งที่ **ไม่ได้** มาจาก cwd (cockpit ป้อนตรง ไม่แตะ project):
  - role definition → `--append-system-prompt-file <runtime/agents/<role>/CLAUDE.md>` (:1631) **ไม่ใช่** CLAUDE.md ใน cwd
  - MCP → `--mcp-config <cockpit file>` + `--strict-mcp-config` (:1659–1663) — user-level `claude mcp add` ไม่เข้า pane
  - plugins → `--plugin-dir` explicit (:1629)
  - hooks (Stop/Notification) → `--settings <hook file>` (:1549) ← **ช่องนี้ inject settings จากไฟล์นอก project ได้ (ใช้กับ rtk ได้)**

### codex pane
- อ่าน **`AGENTS.md`** จาก cwd + walk up (native codex discovery) — cockpit plant ให้ (:1181)

### gemini pane (รันด้วย `agy` = Antigravity)
- อ่าน **`AGENTS.md` / `.agents/`** จาก cwd + walk up — **ไม่อ่าน `GEMINI.md`** (agy retired Gemini CLI เดิม) → reuse ไฟล์เดียวกับ codex (:1109–1112)

### แยกประเภท "ย้ายได้แค่ไหน"

| ไฟล์ | classify | เหตุผล |
|---|---|---|
| task ledger / exports / role-memory / worktrees / transcripts | **ย้ายได้เลย** | central แล้ว ไม่มี CLI ไหน discover — ไม่ต้องทำอะไร |
| `<cwd>/.claude/skills/` (custom skill) | **ย้ายได้ถ้ามี junction** | claude ต้องเห็นใน cwd → ย้ายจริงไป central แล้ว **junction กลับ** `<project>/.claude/skills` (dir → Windows junction ได้ไม่ต้อง admin) |
| `<cwd>/AGENTS.md` (codex/agy) | **ย้ายไม่ได้ตรงๆ** | เป็น **ไฟล์** ไม่ใช่ dir → Windows junction ไม่ได้ (junction = dir เท่านั้น), symlink ไฟล์ต้อง admin/dev-mode. codex/agy walk-up จาก cwd → ต้องมีในหรือเหนือ cwd. **ทางแก้ = gitignore auto** (ไฟล์ marker "do not commit" อยู่แล้ว รกแค่ตอน `git status`) |
| `<cwd>/.claude/settings.json` (rtk) | **ย้ายเข้า `--settings` inject ได้** | cockpit มี channel `--settings <file>` อยู่แล้ว (hook_wiring) → รวม rtk hook เข้าไฟล์นั้นแทนเขียน project settings.json |
| `<cwd>/CLAUDE.md` | **ต้องอยู่ใน repo** | user เป็นเจ้าของ — cockpit ไม่เขียน ไม่ต้องย้าย |
| `docs/design-review · reviews · guides` | **user ตัดสิน** | ไม่ discovery-bound แต่ user อาจ**ตั้งใจ**ให้อยู่ใน repo (design artifact ใน git history) → **ถาม** |

---

## 3. เสนอ migration mapping + ผลกระทบ

### 3.1 ชื่อ + โครง folder กลาง

**ใช้ `~/.agent-takkub` (= `DATA_HOME` เดิม) เป็น single central home — อย่าสร้าง root ที่ 3**

เหตุผล:
- `~/.agent-takkub` = `DATA_HOME` ในโหมด installed อยู่แล้ว (`config._resolve_data_home` :96) — runtime ทั้งหมดอยู่ที่นี่
- `~/.takkub` = legacy (`SETTINGS_HOME` โหมด dev เท่านั้น) — เก็บไว้ backwards-compat, **อย่าเพิ่มของใหม่ลงไป**
- โหมด dev: `DATA_HOME == REPO_ROOT` → central = `agent-takkub/runtime` ซึ่ง**ถูกต้องแล้ว** (repo maintainer เอง)

**โครงที่เสนอ (ของใหม่ที่ต้องย้ายออกจาก project — คือ skills):**
```
<DATA_HOME>/                              # ~/.agent-takkub (installed) | <repo>/ (dev)
├── runtime/                              # (มีแล้ว) ledger, exports, role-memory, transcripts...
│   ├── tasks/<project>/…
│   ├── exports/<date>/<project>/…
│   └── role-memory/<project>/…
├── worktrees/<project>/…                 # (มีแล้ว)
└── project-skills/<project_ns>/          # ★ ใหม่ — custom skill ย้ายมาที่นี่
    └── .claude/skills/<name>/SKILL.md
```
แล้ว junction: `<project>/.claude/skills` → `<DATA_HOME>/project-skills/<project_ns>/.claude/skills`

### 3.2 โมดูลที่ต้องแก้ (จุดต่อจุด)

| โมดูล | แก้อะไร |
|---|---|
| `config.py` | เพิ่ม helper `project_skills_dir(project_ns)` → `DATA_HOME/project-skills/<ns>/.claude/skills` (pattern เดียวกับ `_ledger_dir`) + helper สร้าง junction cross-platform |
| `skill_scan.create_skill` / `settings_window._writable_skill_roots` :2075 | เปลี่ยน target จาก project path → central `project_skills_dir` + เรียก junction ensure. `scan_skills` roots (`_skill_roots_for_project` :114, `_new_role_skill_roots`) ต้อง include central dir ด้วย เพื่อให้ Catalog/Matrix ยังเห็น |
| `spawn_engine._skill_roots_for_project` :114 | เพิ่ม central `project-skills` เข้า roots ที่ inject ตอน spawn (สำหรับ codex/agy appendix + ให้ junction ของ claude ชี้ถูก) |
| `codex_agents_md.ensure_agents_md` :147 | (ไม่ย้ายไฟล์) เพิ่ม auto-gitignore: append `AGENTS.md` ลง `<cwd>/.git/info/exclude` ตอน plant (ไม่แตะ `.gitignore` ของ user ที่ commit) → หายจาก `git status` |
| `rtk_helper.install_rtk` :108 | ทางเลือก: merge rtk hook เข้าไฟล์ `--settings` ของ cockpit (`hook_wiring.ensure_hook_settings_file`) แทนเขียน `<project>/.claude/settings.json` — แต่ **ต้องเช็คว่า rtk hook ทำงานผ่าน `--settings` layer ได้จริง** (claude รวม hook จากทุก `--settings` source) |
| `CLAUDE.md` (cockpit) | (a) แก้ pipeline example `save shots to runtime/exports/...` → `save shots to $TAKKUB_ARTIFACTS_DIR/screenshots/` (absolute, central) · (b) ตัดสินใจ docs/ location (ดู 3.4) |
| `.gitignore` audit | ตรวจว่า user project ควรมี entry อะไรบ้าง (fallback ถ้า junction/exclude ใช้ไม่ได้) |

### 3.3 Junction strategy ต่อ OS — **มี helper reuse ได้แล้ว**

**`worktree_manager._make_link(src, dst)`** ทำ cross-platform link อยู่แล้ว
(`user_profile.provision_shared_profile` :122 เรียกใช้):
- **Windows:** `_winapi.CreateJunction` — junction, **ไม่ต้อง admin** (dir เท่านั้น — ตรงกับ `.claude/skills` ที่เป็น dir พอดี)
- **macOS/Linux:** `os.symlink` — ไม่ต้อง privilege พิเศษ

→ migration ของ skills **reuse `_make_link` ได้เลย** ไม่ต้องเขียน junction logic ใหม่ (ตรงกับ policy "อย่า reinvent")

**ข้อควรระวัง cross-platform** (จาก `worktree_manager` :70–72, user_profile :214):
- recursive delete ผ่าน junction จะลบ **ของจริงปลายทาง** ด้วย → ตอน "ลบ project" / cleanup ต้อง unlink junction **ก่อน** ลบ folder (worktree_manager มี pattern นี้แล้ว ลอกมาใช้)

### 3.4 ของเดิมที่ user มีอยู่ migrate ยังไง

| ของ | วิธี migrate |
|---|---|
| `<project>/.claude/skills/` ที่ cockpit สร้างไว้ก่อนหน้า | one-time: move → central + junction กลับ. **ระวัง:** ถ้า skill นั้น user commit ไป repo แล้ว → เป็น skill ของ project จริง **อย่าย้าย** (แยกด้วย git-tracked check: tracked = ของ user, untracked = ของ cockpit) |
| `<project>/AGENTS.md` (marker) | ลบทิ้งได้ — spawn ครั้งหน้า replant เอง; หรือแค่ add เข้า `.git/info/exclude` |
| `<project>/.claude/settings.json` rtk hook | ถ้าย้ายเข้า `--settings`: ลบ rtk entry ออกจาก project settings.json (เก็บ key อื่นของ user ไว้) |

### 3.5 ความเสี่ยง + ประเด็นที่ **ต้องถาม user ก่อนตัดสิน**

1. **docs/design-review, docs/reviews, docs/guides, docs/system-overview — user ตั้งใจให้อยู่ใน repo ไหม?**
   - เหตุผลให้อยู่: เป็น design/review artifact ที่อยากเก็บใน git history ของ project · html เปิดดูจากใน repo สะดวก
   - เหตุผลให้ย้าย: รก repo, ไม่ใช่ code
   - **ต้องถาม** — ถ้า user บอก "อยู่ repo ได้" → กลุ่ม C นี้ไม่ต้องแตะ, เหลือแค่ A1/A2/A3 + fix screenshot wording

2. **`<project>/.claude/settings.json` (rtk hook) — user อยาก commit ให้ทีมใช้ร่วมไหม?**
   - ถ้าใช่ → **อย่าย้าย** (เป็น project config ที่ตั้งใจ share) — ปล่อยไว้
   - ถ้าไม่ (แค่ personal convenience) → ย้ายเข้า `--settings` inject

3. **rtk-hook-via-`--settings` ต้อง verify จริง** — ยังไม่ได้พิสูจน์ว่า claude รวม PreToolUse hook จาก `--settings` file (นอก project) แล้วยิงตอน Bash จริง — **ต้องเทสก่อน** commit วิธีนี้ (ไม่งั้น rtk เงียบ token พุ่ง)

4. **git-tracked vs untracked skill** — logic แยก "skill ของ user (commit แล้ว)" ออกจาก "skill ของ cockpit (untracked)" ต้องแม่น ไม่งั้น migration ลากของ user ไป central ผิด

5. **Multi-provider (#103):** central skills ต้องทำงานกับ **ทุก provider** — claude (junction cwd), codex/agy (appendix จาก `render_skill_appendix` อ่าน central roots). เช็คว่า `_skill_roots_for_project` ครอบคลุมทั้ง claude junction path + codex/agy appendix path

6. **Cross-platform test คู่:** junction (win) + symlink (mac) ต้องเทสทั้ง 2 ฝั่ง — `_make_link` มี branch ครบแล้วแต่ flow ใหม่ (project-skills + cleanup unlink) ต้อง cover ทั้งคู่ใน CI matrix

---

## ภาคผนวก — สรุป call-graph หลักฐาน

- `config._resolve_data_home` :96 → `DATA_HOME` (dev=REPO_ROOT / installed=~/.agent-takkub)
- `config._resolve_settings_home` :117 → `SETTINGS_HOME` (dev=~/.takkub / installed=DATA_HOME)
- write ลง cwd จริง: `skill_scan.create_skill` :129 · `codex_agents_md.ensure_agents_md` :188 · `rtk_helper.install_rtk` :125
- discovery flags: `spawn_engine` :1526 (`--setting-sources project,local`) · :1631 (`--append-system-prompt-file`) · :1659 (`--mcp-config` + `--strict`) · :1549 (`--settings` hook channel)
- reusable link helper: `worktree_manager._make_link` (win junction / posix symlink) — ใช้แล้วที่ `user_profile.provision_shared_profile` :122
