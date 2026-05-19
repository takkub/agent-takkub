# agent-takkub

> Desktop cockpit สำหรับ orchestrate Claude Code dev team หลาย pane พร้อมกัน บน Windows
> PyQt6 GUI + Windows native PTY + `takkub` CLI ที่ agents ใช้คุยกันเองได้

---

## สรุประบบสั้นๆ

`agent-takkub` คือ **desktop app** ที่เปิด Claude Code หลายตัวพร้อมกันในรูปแบบ pane grid ให้ทำงานเป็นทีม dev จริงๆ:

```
┌──────────────────┬──────────────────┐
│                  │  Frontend pane   │
│                  ├──────────────────┤
│   Lead pane      │  Backend pane    │
│  (orchestrator)  ├──────────────────┤
│                  │  QA pane         │
│                  ├──────────────────┤
│                  │  Reviewer pane   │
└──────────────────┴──────────────────┘
```

- **Lead** = claude ตัวหลัก (อยู่ซ้าย) สั่งงาน, วางแผน, รับ report
- **Teammates** = specialist claude (frontend / backend / mobile / devops / designer / qa / reviewer) ที่ spawn ตามคำสั่ง Lead
- **takkub CLI** = ทุก pane เรียกคำสั่งหากันเองได้: `takkub assign --role backend "<task>"`, `takkub send --to qa "<msg>"`, `takkub done`
- **Multi-project tabs** = เปิดหลาย project พร้อมกันใน tab แยก, แต่ละ tab มี Lead + teammates ของตัวเอง, ไม่ cross-talk
- **Auto-everything** — session resume on restart, stuck-pane auto-recover, MCP pre-warm, decision log to Obsidian vault, daily digest, resume brief, hook noise meter

**ไม่ใช่** SaaS, ไม่ส่ง code ออกนอกเครื่อง, ทำงานบน `claude` CLI ที่ login แล้ว (Max OAuth หรือ API key — ตามที่คุณตั้งไว้ใน Claude Code)

---

## Prerequisites — สิ่งที่ต้องมีก่อนติดตั้ง

ติดตั้งครั้งเดียวต่อเครื่อง:

### 1. Python 3.11 หรือสูงกว่า
- ดาวน์โหลด: <https://www.python.org/downloads/>
- ตอน install **ติ๊ก "Add Python to PATH"** (สำคัญที่สุด)
- ตรวจ: เปิด terminal ใหม่ → `python --version` ต้องโชว์ `Python 3.11.x` หรือสูงกว่า

### 2. Node.js (LTS) + npm
- ดาวน์โหลด: <https://nodejs.org/> เลือก LTS
- ใช้สำหรับติดตั้ง `claude` CLI และ run browser MCPs
- ตรวจ: `node --version` ต้องโชว์ `v20.x` หรือสูงกว่า, `npx --version` ต้องโชว์เลข version

### 3. Claude Code CLI
- ติดตั้ง: `npm install -g @anthropic-ai/claude-code`
- รัน `claude` ครั้งแรกเพื่อ login (Claude Max OAuth หรือใส่ API key)
- ตรวจ: `claude --version` ต้องโชว์ version

### 4. Git
- Windows: <https://git-scm.com/download/win>
- ใช้สำหรับ clone repo

### (ทางเลือก) 5. Obsidian
- ดาวน์โหลด: <https://obsidian.md/>
- ถ้าใช้ → cockpit จะ auto-mirror decision logs + hot snapshot + daily digest + resume briefs เข้า vault
- ไม่ใช้ก็ได้ — cockpit ทำงานปกติ แค่ skip vault mirror

---

## One-shot install (Windows, recommended for เครื่องใหม่)

`scripts/install.ps1` ลงทุกอย่างที่ระบบนี้ใช้ในรอบเดียว
ตัวไหนลงไว้แล้วจะ **skip** อัตโนมัติ ส่ง `-Update` เพื่อ upgrade

```powershell
git clone https://github.com/takkub/agent-takkub.git
cd agent-takkub
.\scripts\install.ps1
```

ถ้า PowerShell execution policy บล็อก หรืออยาก double-click → ใช้
`.bat` แทน (wrapper บางๆ ที่ตั้ง `-ExecutionPolicy Bypass` ให้รอบเดียว):

```bat
scripts\install.bat
scripts\install.bat -Update
```

สิ่งที่ script ลงให้ (เรียงตาม phase):

| Phase | สิ่งที่ลง | ทำไม |
|---|---|---|
| 1 | Python 3.11+, Git, Node.js LTS, Chrome, GitHub CLI | runtime + ผูก git update flow + Chrome สำหรับ chrome-devtools MCP |
| 2 | npm registry → `registry.npmjs.org` | กัน corporate proxy block MCP fetch |
| 3 | Claude Code CLI, OpenAI Codex CLI | backend ของ Lead pane + Codex pane |
| 4 | Claude plugins: superpowers, agent-skills, ECC, Pordee | skills + reviewers + workflow utilities ที่ agents ใช้ผ่าน `/skill-name` |
| 4b | MCP servers: `@playwright/mcp`, `chrome-devtools-mcp` + Playwright Chromium (~150 MB) | pre-warm npm cache + Playwright browser → Lead pane spawn ครั้งแรกไม่ต้องรอ MCP download |
| 5 | rtk (Rust Token Killer) | optional — ลด token usage 60-90% ของ shell command output |
| 6 | clone agent-takkub + `pip install -e .` | cockpit เอง |
| 7 | `~/.takkub/role-providers.json` (empty `{}`), Obsidian vault skeleton | per-role provider config + vault placeholder สำหรับ session mirror |

> Login (`claude login` / `codex login`) ไม่ได้รวมใน script — รันเองหลัง install เสร็จ (เปิด browser OAuth)

**Flags:**

| Flag | ทำอะไร |
|---|---|
| (none) | ลงเฉพาะที่ยังไม่มี |
| `-Update` | re-install / upgrade ทุกตัว ดึง `git pull` cockpit ล่าสุดด้วย |
| `-SkipMCPPrewarm` | ข้าม Phase 4b — MCP packages download อัตโนมัติตอน Lead pane spawn แทน |
| `-VaultDir ""` | ข้ามการสร้าง Obsidian vault skeleton |

หลังจบ script จะ print **summary** ว่าตัวไหน installed / upgraded / skipped / failed
รันได้ซ้ำได้ทุกเมื่อ — idempotent, re-runnable

---

## Quick Install (สามขั้น — manual path)

```bat
git clone git@github.com:takkub/agent-takkub.git
cd agent-takkub
agent-takkub.bat
```

`agent-takkub.bat` ทำให้อัตโนมัติ:

1. ตรวจ Python + Claude CLI + Node อยู่ใน PATH
2. สร้าง `.venv` + `pip install -e .` (download PyQt6 + Chromium ~150 MB ใช้เวลา 1–3 นาทีครั้งแรก)
3. Copy `projects.json.example` → `projects.json` เปิด notepad ให้คุณแก้ paths ของ project
4. Launch cockpit window

ครั้งถัดไปรัน `agent-takkub.bat` แล้วเปิดทันที (ไม่ตรวจ setup ซ้ำ)

---

## Detailed Install (ทีละขั้นถ้าอยากเข้าใจ)

### Step 1: Clone repo

```bat
cd C:\Users\<you>\Projects
git clone git@github.com:takkub/agent-takkub.git
cd agent-takkub
```

ถ้าไม่มี SSH key set up ให้ใช้ HTTPS:
```bat
git clone https://github.com/takkub/agent-takkub.git
```

### Step 2: สร้าง Python virtual environment

```bat
python -m venv .venv
```

จะได้ folder `.venv` ใน repo (gitignore แล้ว) — ใส่ Python interpreter + libs ทั้งหมดที่ cockpit ต้องการ ไม่ปนกับ Python ตัวอื่นในเครื่อง

### Step 3: ติดตั้ง dependencies

```bat
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -e .
```

ครั้งแรก pip จะดาวน์โหลด:
- **PyQt6** + **PyQt6-WebEngine** (~150 MB เพราะมี Chromium binary)
- **pywinpty** (Windows native PTY wrapper)
- **pyte** (terminal emulator)

ใช้เวลา 1–3 นาที ขึ้นอยู่กับ network

### Step 4: ตั้งค่า projects.json

```bat
copy projects.json.example projects.json
notepad projects.json
```

แก้ตามตัวอย่าง (เปลี่ยน paths เป็น project ของคุณ):

```json
{
  "active": "myproject",
  "projects": {
    "myproject": {
      "description": "My web app",
      "paths": {
        "web": "C:/Users/me/Projects/myproject/web",
        "api": "C:/Users/me/Projects/myproject/api"
      },
      "presets": ["frontend", "backend"]
    }
  }
}
```

**Field reference:**
- `active` — ชื่อ project ที่ default เปิดเมื่อ launch cockpit
- `paths` — key มาตรฐาน (`web` / `api` / `mobile` / `infra` / etc.) → ใช้สำหรับ role-aware cwd
  - frontend / designer → `web` first
  - backend → `api` first
  - mobile → `mobile` first, fallback `web`
  - devops → `api` / `infra`
  - qa / reviewer → first path
- `presets` — รายชื่อ role ที่จะ auto-spawn 15 วินาทีหลัง Lead boot (เว้น 3s ต่อ role)

### Step 5: เปิด cockpit

```bat
.venv\Scripts\pythonw.exe -m agent_takkub
```

หรือ
```bat
agent-takkub.bat
```

หน้าต่าง PyQt6 จะเปิดขึ้นมา, Lead pane จะ boot อัตโนมัติ และถ้ามี `presets` ก็จะ auto-spawn teammates

---

## First Launch — what happens

1. **Lead pane** เปิดขึ้นมาเต็มจอ + claude bootstrap ~5 วินาที
2. ถ้ามี **trust folder modal** จาก claude → cockpit auto-press Enter ให้
3. **Preset teammates** spawn ตามลำดับใน projects.json (3 วินาทีต่อ role)
4. **Browser MCPs** (Playwright + Chrome DevTools) pre-warm in background — ครั้งแรกใช้เวลา 10–30s ดาวน์โหลด, ครั้งต่อไป instant
5. ถ้ามี **session snapshot** จาก crash/restart ครั้งก่อน → cockpit re-spawn panes พร้อม `--continue`

---

## Daily usage

### สั่ง Lead จาก main pane

พิมพ์ใน Lead pane ตรงๆ เช่น:
```
ช่วยเพิ่ม endpoint /login ที่ backend + form /login ที่ frontend
```

Lead จะใช้ `takkub assign` spawn teammates เองตาม role ที่ต้องการ

### `takkub` CLI (รันจาก pane ใดก็ได้)

```bash
takkub list                                          # ดูสถานะทุก pane
takkub assign --role backend --cwd <path> "<task>"   # spawn (ถ้ายังไม่เปิด) + ส่ง task
takkub send --to frontend "API ใช้ POST /auth/login" # peer message (CC Lead อัตโนมัติ)
takkub close --role qa                               # ปิด pane
takkub close-all                                     # ปิด teammates ทั้งหมด (Lead รอด)
takkub done [note]                                   # teammates report เสร็จ → ปิดตัวเอง
takkub search "<query>" [--days N] [--all]           # grep past Claude conversations
```

### หน้าต่าง multi-project

- กดปุ่ม `+` มุมขวาบนของ tab strip → picker เลือก project เพิ่ม
- กด `x` บน tab → confirm dialog → ปิด Lead + teammates ของ project นั้น
- ทุก project มี Lead + teammates ของตัวเอง, แยก audit trail, ไม่ cross-talk

### Finish Job

ปุ่ม **✅ Finish Job** ขวาล่าง status bar:
1. Lead เขียน structured summary (Accomplished / Blockers / Next Steps)
2. Append **daily digest** ลง `<vault>/05-Daily/<date>.md` (ถ้ามี vault)
3. ปิด teammates ทั้งหมด (Lead รอดเพื่ออ่าน summary)

---

## Optional Integrations

### Obsidian vault

ถ้ามี Obsidian vault ที่มี folder `01-Projects/` ภายใน → cockpit auto-mirror:

| ไฟล์ที่เขียน | ตอนไหน | เนื้อหา |
|---|---|---|
| `<vault>/01-Projects/<project>/sessions/<date>T<time>-<role>.md` | ทุกครั้งที่ teammate `takkub done` | decision log จากการทำงาน |
| `<vault>/hot.md` | ทุก 60 วินาที + ทุก done event | live snapshot: active project, panes, recent done, hook noise, friction |
| `<vault>/05-Daily/<date>.md` | กด Finish Job | per-project digest: sessions + decisions today |
| `<vault>/07-AI-Command-Center/briefs/<project>-<date>T<time>.md` | ปิด cockpit | last 20 exchanges as resume brief |

**Resolution order** ของ vault path:
1. `$TAKKUB_VAULT_DIR` (explicit override)
2. `~/WebstormProjects/second-brain` (default, ถ้ามี `01-Projects/` ข้างใน)
3. ไม่มี → skip silently

### PMS MCP (optional)

ใส่ bearer token เข้า `runtime/shared-mcp.json` ตรงๆ (ไฟล์ gitignore แล้ว) หรือเรียก `write_shared_mcp_config(token)` จาก Python — ทุก pane จะใช้ pms MCP tools (`mcp__pms__pms_*`) ได้ผ่าน `--mcp-config` ที่ orchestrator inject ทุก spawn (ไม่มี UI button — token ไม่ rotate บ่อย ปกติตั้งครั้งเดียวจบ)

### Browser MCPs (auto, ไม่ต้อง setup)

Cockpit auto-inject:
- `@playwright/mcp@0.0.75` — smoke tests, e2e
- `chrome-devtools-mcp@0.26.0` — runtime debug ของ web app

ทุก pane เห็น `mcp__playwright__*` และ `mcp__chrome-devtools__*` tools ได้

---

## Auto features (ทำงานเองในพื้นหลัง)

1. **Session resume** — ปิด cockpit → snapshot ทุก active pane → เปิดใหม่ → re-spawn พร้อม `--continue` (ใน 1 ชั่วโมง)
2. **Stuck-pane auto-recover** — teammate working > 10 นาที ไม่มี output → auto close + respawn พร้อม `--continue`
3. **Auto-respawn on crash** — claude.exe ตาย unexpected → spawn ใหม่ใน slot เดิมไม่เกิน 2 ครั้ง
4. **MCP pre-warm** — boot cockpit → npx download/start playwright + chrome-devtools ใน background
5. **80% context warning** — pane ไหน token ใช้ > 80% → toast + status bar
6. **Multi-tab token chip** — tab title โชว์ peak usage `<project> · 52k/200k`
7. **ECC plugin noise muted** — auto-inject `ECC_GATEGUARD=off` + `ECC_DISABLED_HOOKS` กัน hook spam
8. **Auto-trust folder** — claude แสดง trust modal → cockpit press Enter ให้
9. **Decision log** — ทุก `takkub done` → markdown file + Obsidian mirror

---

## Chatlog mining (read-only insights จาก Claude sessions)

Cockpit อ่าน `~/.claude/projects/<encoded>/*.jsonl` (Claude Code's session logs) เพื่อแสดง:

| What | Where |
|---|---|
| **Hook noise meter** | `<vault>/hot.md` "Hook noise today" section — นับ ECC GateGuard / cost-critical / loop-warning ที่ fire วันนี้ |
| **Friction heatmap** | `<vault>/hot.md` "Friction today" — นับ user corrections (ไม่ใช่, ผิด, พังเลย) + tool retry storms |
| **Decision timeline** | Daily digest "Decisions today" — H2-headed assistant messages |
| **Resume brief** | ปิด cockpit → per-project last 20 exchanges |
| **Search** | `takkub search "<query>"` — grep ทุก project + ทุก session |

ทั้งหมด read-only — ไม่แตะไฟล์ของ Claude

---

## Environment variables (optional)

| Var | Effect |
|---|---|
| `TAKKUB_VAULT_DIR` | path ไป Obsidian vault (override default `~/WebstormProjects/second-brain`) |
| `TAKKUB_SETTING_SOURCES` | claude `--setting-sources` flag (default `project,local` กัน claude-obsidian SessionStart crash) |
| `TAKKUB_ECC_FULL=1` | ปิด ECC noise mute (เปิด ECC hooks ครบทุกตัว) |
| `TAKKUB_TEAMMATE_MODEL` | model สำหรับ teammates (default `claude-sonnet-4-6`) |
| `TAKKUB_TEAMMATE_EFFORT` | effort level (default `medium`) |
| `TAKKUB_AUTO_REMOTE_CONTROL=0` | skip auto `/remote-control` ใน Lead |
| `TAKKUB_ALLOW_TASK=1` | un-block built-in `Task` tool ให้ panes (default block, ใช้ `takkub assign` แทน) |

ตั้ง env vars **ก่อน** launch cockpit จาก terminal เดียวกัน, หรือใส่ใน Windows system env vars

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Python is not on PATH` | re-install Python กับ option "Add Python to PATH" |
| `claude CLI is not on PATH` | `npm install -g @anthropic-ai/claude-code` แล้ว `claude` ครั้งหนึ่งเพื่อ login |
| Cockpit เปิดแล้วปิดทันที | รัน `.venv\Scripts\python.exe -m agent_takkub` (ไม่ใช่ `pythonw`) เพื่อดู error ใน console |
| `takkub: command not found` ใน pane | `bin\takkub` shim ต้องอยู่ — เช็คว่าไฟล์มีจริงใน clone |
| Thai สระแสดงไม่ครบ | upgrade > v0.3.0 (xterm.js terminal) |
| Playwright MCP "ไม่ connect" | restart cockpit 1 ครั้ง (per-spawn migration + version pin จะ kick in) |
| Lead spawn ใน wrong dir | ตรวจ `projects.json` → `paths` แล้วเพิ่ม `"lead": "web"` |
| Browser MCPs prompt ทุกครั้ง | ปกติ — claude ขอ allow ครั้งแรกของ session ใหม่ |
| `runtime/shared-mcp.json` race | restart cockpit (one-time, idempotent re-write) |
| ECC GateGuard fact-force ขึ้นทุก Edit | cockpit auto-mute ตั้งแต่ v0.3.5+ — ถ้ายังขึ้น = pane เก่า, ปิด+spawn ใหม่ |

---

## Project structure

```
agent-takkub/
├── README.md                     # ไฟล์นี้
├── CLAUDE.md                     # Lead's system prompt (spawn-time)
├── projects.json                 # active project + paths + presets
├── pyproject.toml                # PyQt6 + dependencies
├── agent-takkub.bat              # one-click launcher (setup + launch)
├── .claude/agents/               # 7 default role CLAUDE.md
├── bin/takkub                    # POSIX shim → python -m agent_takkub.cli
├── bin/takkub.cmd                # Windows cmd shim
├── runtime/                      # gitignored — port file, events.log, sessions, snapshots
├── src/agent_takkub/
│   ├── app.py                    # PyQt entry point
│   ├── main_window.py            # QTabWidget, status bar, Finish Job
│   ├── project_tab.py            # per-tab Lead + teammate stack
│   ├── agent_pane.py             # pane header + state + terminal stack
│   ├── terminal_widget.py        # xterm.js terminal in QWebEngineView
│   ├── pty_session.py            # pywinpty wrapper + reader/writer threads
│   ├── orchestrator.py           # spawn/assign/send/close/done + auto-features
│   ├── cli_server.py             # QTcpServer for takkub CLI
│   ├── cli.py                    # `takkub` CLI client
│   ├── chatlog_scanner.py        # read-only walker over Claude session jsonl
│   ├── shared_dev_tools.py       # MCP shared config (pms + browser MCPs)
│   ├── rtk_helper.py             # rtk hook one-click installer
│   ├── logs_panel.py             # bottom dock: tail events.log
│   ├── config.py                 # projects.json + runtime/ helpers
│   ├── roles.py                  # default role registry
│   └── token_meter.py            # JSONL session reader + context budget
└── tests/                        # 231 unit tests, no GUI required
```

---

## License

MIT — ดู `LICENSE`

---

## Contributing / Bug reports

GitHub Issues: <https://github.com/takkub/agent-takkub/issues>

PR welcome, ลอง `python -m pytest tests/` ก่อน push (ต้องผ่านครบทุก test)
