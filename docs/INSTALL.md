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
- **Teammates** = specialist agents ที่ spawn ตามคำสั่ง Lead — claude-backed (frontend / backend / mobile / devops / qa / reviewer / critic) + non-claude panes (**codex** = OpenAI Codex CLI, **gemini** = Google Antigravity CLI `agy`) + ad-hoc **shell** · *codex/gemini เป็น optional — ถ้าไม่ได้ติดตั้ง CLI ของมัน role นั้นจะรันด้วย Claude แทนอัตโนมัติ (เสีย model diversity แต่ไม่ติดขัด)*
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

### (ทางเลือก) 6. AI CLIs ตัวอื่น — สำหรับ role `codex` / `gemini` (model diversity)
cockpit ใช้ **Claude อย่างเดียวก็ทำงานครบ** — Lead + ทุก role รันด้วย Claude ได้ 2 ตัวนี้เป็น optional ให้ "สมองที่ 2/3" จากโมเดลอื่นเวลา cross-check:
- **OpenAI Codex CLI** — `npm install -g @openai/codex` แล้ว `codex login` (role `codex`)
- **Google Antigravity CLI (`agy`)** — ลงที่ <https://antigravity.google/download> (native installer ลง `%LOCALAPPDATA%\agy\bin`) แล้วรัน `agy` 1 ครั้งทำ Google Sign-In (role `gemini`; แทน Gemini CLI เดิมที่ Google ปิด 18 มิ.ย. 2026)
- **ไม่ลงก็ได้** — role `codex`/`gemini` จะรันด้วย Claude แทนอัตโนมัติ (substitution) ไม่ติดขัด แค่ไม่ได้มุมมองจากโมเดลอื่น
- `scripts/install.ps1` ลง codex + agy ให้แบบ best-effort อยู่แล้ว

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
| 3 | Claude Code CLI (required) · OpenAI Codex CLI + Antigravity CLI `agy` (optional) | Claude = Lead + ทุก claude role · Codex/agy = backend ของ role codex/gemini (best-effort install — ไม่มีก็รันเป็น Claude แทน) |
| 4 | Claude plugins: superpowers, agent-skills, Pordee | skills + reviewers + workflow utilities ที่ agents ใช้ผ่าน `/skill-name` (ECC ไม่ลงโดยตั้งใจ — SessionStart hook ทำ pane crash + กิน ~31k tok/session) |
| 4b | MCP servers: `@playwright/mcp`, `chrome-devtools-mcp` + Playwright Chromium (~150 MB) | pre-warm npm cache + Playwright browser → Lead pane spawn ครั้งแรกไม่ต้องรอ MCP download |
| 5 | rtk (Rust Token Killer) | optional — ลด token usage 60-90% ของ shell command output |
| 6 | clone agent-takkub + `pip install -e .` | cockpit เอง |
| 7 | `~/.takkub/role-providers.json` (empty `{}`), Obsidian vault skeleton | per-role provider config + vault placeholder สำหรับ session mirror |

> Login ไม่ได้รวมใน script — รันเองหลัง install เสร็จ (เปิด browser OAuth):
> `claude login` (required) · `codex login` (optional) · `agy` (optional — first run ทำ Google Sign-In)
> ติดตั้ง agy ผ่าน script แบบ best-effort; ถ้าพลาด ลงเองที่ <https://antigravity.google/download> แล้วรัน `agy` 1 ครั้ง

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

## One-shot install (macOS / Linux)

`scripts/install.sh` คือคู่ขนานของ `install.ps1` สำหรับ macOS (Apple Silicon / Intel)
และ Linux — ลงทุก phase เดียวกัน, ข้ามตัวที่มีแล้ว, idempotent

```bash
git clone https://github.com/takkub/agent-takkub.git
cd agent-takkub
bash scripts/install.sh          # หรือ double-click scripts/install.command ใน Finder
```

ความต่างจากฝั่ง Windows:

- **System tools** ลงผ่าน **Homebrew** (`python@3.12`, `git`, `node`, Chrome cask, `gh`) แทน winget
  ถ้าไม่มี Homebrew → ข้าม phase นั้นแล้วบอกวิธีติดตั้ง brew
- **Node ต้อง v20+** — ถ้า node บนเครื่องเก่าเกินไปและ brew ใช้ไม่ได้ script จะ
  **โหลด Node LTS ตรงจาก nodejs.org ลง `~/.local` ให้อัตโนมัติ** (ไม่ต้องใช้ sudo)
- **Cockpit** ลงด้วย `uv` ถ้ามี (สร้าง `.venv` + editable install) ไม่งั้น fallback เป็น `python3 -m venv` + pip

**Flags:**

| Flag | ทำอะไร |
|---|---|
| (none) | ลงเฉพาะที่ยังไม่มี |
| `--update` | re-install / upgrade ทุกตัว |
| `--skip-mcp-prewarm` | ข้าม Phase 4b (MCP download อัตโนมัติตอน spawn แทน) |
| `--skip-rtk` | ข้าม Phase 5 (ไม่ลง rtk) |
| `--vault-dir <path>` | ที่ตั้ง Obsidian vault skeleton (default `~/second-brain`) |
| `--no-vault` | ข้ามการสร้าง vault skeleton |

> **rtk** ลงจาก **prebuilt binary ของ `github.com/rtk-ai/rtk`** ตรงเข้า `~/.local/bin` (ไม่ต้องมี Rust)
> — ⚠️ ห้าม `cargo install rtk` เพราะ crate ชื่อ `rtk` บน crates.io เป็นคนละตัว (Rust Type Kit) ไม่ใช่ Token Killer

Login ไม่รวมใน script (เหมือนฝั่ง Windows) — รันเองหลังเสร็จ:
`claude` (required) · `codex login` (optional) · `agy` (optional)
แล้วเปิด cockpit ด้วย `.venv/bin/agent-takkub`

---

## Quick Install (สามขั้น — manual path)

```bat
git clone https://github.com/takkub/agent-takkub.git
cd agent-takkub
agent-takkub.bat
```

(มี SSH key อยู่แล้วใช้ `git@github.com:takkub/agent-takkub.git` แทนได้)

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
  - frontend → `web` first
  - backend → `api` first
  - mobile → `mobile` first, fallback `web`
  - devops → `api` / `infra`
  - qa / reviewer / critic → first path
- `presets` — รายชื่อ role ที่จะ auto-spawn 15 วินาทีหลัง Lead boot (เว้น 3s ต่อ role)

### Step 4b: AI-generated project rules (optional)

cockpit สามารถสร้าง `CLAUDE.md` สำหรับแต่ละ project โดยใช้ AI — ไม่ต้องเขียนเอง

**New project flow (เพิ่ม project ใหม่):**
1. กดปุ่ม **＋ Add Project** ใน status bar
2. เลือก **"New project (AI rules)"** → ใส่คำอธิบาย project สั้นๆ
3. cockpit รัน Claude Code headless สร้าง `CLAUDE.md` ให้ (ใช้เวลา ~15–60 วินาที)
4. preview + แก้ไขใน editor dialog ก่อน save หรือกด **🔄 Regenerate** ถ้าไม่พอใจ
5. `<project-root>/CLAUDE.md` ถูกสร้างและ inject เข้า Lead pane ทุก session อัตโนมัติ

**Edit rules ภายหลัง:**
- กดปุ่ม **✏ Rules** ใน status bar ขณะที่ project นั้น active
- แก้ไขใน editor dialog → Save หรือ Regenerate จาก prompt ใหม่

**Auto-injection:** Lead spawn ทุกครั้งจะโหลด `CLAUDE.md` ของ project (cap 3000 chars) เข้า context อัตโนมัติ — Lead รู้ codebase conventions โดยไม่ต้องพิมพ์ซ้ำ

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
takkub doctor [--fix] [--json]                       # ตรวจ env: claude/node/plugins/mcps/projects + fix hint
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
2. `~/second-brain` (default, ถ้ามี `01-Projects/` ข้างใน)
3. ไม่มี → skip silently

### Shared MCPs — user-level inheritance (auto, ไม่ต้อง setup)

cockpit boot ทุกครั้ง อ่าน `~/.claude.json` → merge เฉพาะ MCPs ที่อยู่ใน allowlist เข้า `runtime/shared-mcp.json`:

| MCP | included by default |
|---|---|
| user MCP ใดๆ จาก `~/.claude.json` | ❌ — allowlist ว่างโดย default (ไม่ trust อัตโนมัติ) เพิ่มผ่าน `takkub mcp add` / Tools dialog |
| entries ที่มี `Authorization` header / env `TOKEN/KEY/SECRET` / DSN `user:pass@` ใน args | ❌ skip อัตโนมัติ + warning (generic secret-guard) |
| `playwright`, `chrome-devtools` | ✅ cockpit force-inject (ให้ qa/critic/designer) |

browser pane เห็น `mcp__playwright__*`, `mcp__chrome-devtools__*`

---

## Auto features (ทำงานเองในพื้นหลัง)

1. **Session resume** — ปิด cockpit → snapshot ทุก active pane → เปิดใหม่ → re-spawn พร้อม `--continue` (ใน 1 ชั่วโมง)
2. **Stuck-pane auto-recover** — teammate working > 10 นาที ไม่มี output → auto close + respawn พร้อม `--continue`
3. **Auto-respawn on crash** — claude.exe ตาย unexpected → spawn ใหม่ใน slot เดิมไม่เกิน 2 ครั้ง
4. **MCP pre-warm** — boot cockpit → npx download/start playwright + chrome-devtools ใน background
5. **80% context warning** — pane ไหน token ใช้ > 80% → toast + status bar
6. **Multi-tab token chip** — tab title โชว์ peak usage `<project> · 52k/200k`
7. **ECC plugin noise muted** (defensive) — cockpit ไม่ลง ECC เอง แต่ถ้ามีติดมาจากที่อื่น จะ auto-inject `ECC_GATEGUARD=off` + `ECC_DISABLED_HOOKS` กัน hook spam
8. **Auto-trust folder** — claude แสดง trust modal → cockpit press Enter ให้
9. **Decision log** — ทุก `takkub done` → markdown file + Obsidian mirror
10. **Cross-tab done notification** — teammate ใน background tab รัน `takkub done` → Lead เห็น `[<role> done] in <project>` flash บน status bar ทุก tab (ไม่ต้องสลับไปดู)
11. **Done notice queue** — teammate `done()` ขณะ target Lead ไม่ alive → notice ค้างใน queue → flush เข้า input ตอน Lead respawn

---

## Security

| What | Where |
|---|---|
| **Lead capability token** — auth gate กัน TCP bypass | `cli_server.py` requires `TAKKUB_LEAD_TOKEN` env (auto-injected into Lead pane only) สำหรับ spawn/assign/close/close-all teammate panes ที่พยายาม connect socket ตรงๆ → reject |
| **Role/project name validation** | `config.validate_name()` regex `^[a-z0-9][a-z0-9_-]{0,63}$` กัน path traversal ก่อน filesystem touch |
| **Role-aware cwd guard** | `orchestrator._cwd_within_project()` REPO_ROOT exception ใช้กับ Lead เท่านั้น — teammate spawn ใน cockpit repo ไม่ได้ |
| **MCP config** | cockpit merge เฉพาะ MCPs ที่อยู่ใน allowlist (`ensure_user_mcps`) เข้า `runtime/shared-mcp.json` + sanitize: skip entry ที่มี bearer token / DSN creds พร้อม warning ใน log *(หมายเหตุ: `--strict-mcp-config` ไม่ได้บล็อก user-level `~/.claude.json` MCP loading ใน Claude เวอร์ชันปัจจุบัน — allowlist คุมแค่ไฟล์ shared-mcp.json ที่ cockpit เขียนเอง)* |
| **No plaintext credentials** | credential-bearing MCP (bearer header / DSN `user:pass@`) ถูก skip โดย generic secret-guard — ไม่เขียนลง shared runtime; ผู้ใช้ setup credential เองนอก cockpit |

Audit trail: `REVIEW_<date>.md` ที่ repo root + `runtime/sessions/<date>/<project>/codex-*.md` (codex security reviews)

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
| `TAKKUB_VAULT_DIR` | path ไป Obsidian vault (override default `~/second-brain`) |
| `TAKKUB_SETTING_SOURCES` | claude `--setting-sources` flag (default `project,local` กัน claude-obsidian SessionStart crash) |
| `TAKKUB_ECC_FULL=1` | ปิด ECC noise mute (เปิด ECC hooks ครบทุกตัว) |
| `TAKKUB_TEAMMATE_MODEL` | บังคับ model ให้ teammate **ทุก role** (override per-role tier; default per-role: reviewer/critic→`claude-opus-4-8`, อื่นๆ→`claude-sonnet-5`) |
| `TAKKUB_TEAMMATE_EFFORT` | บังคับ effort ให้ **ทุก role** (override per-role tier; default per-role: reviewer/critic/backend/devops→`high`, อื่นๆ→`medium`) |
| `TAKKUB_TEAMMATE_FALLBACK` | claude `--fallback-model` ของ teammate ตอน overload/not-found (default `claude-haiku-4-5`, `""` = ปิด) |
| `TAKKUB_LEAD_FALLBACK` | claude `--fallback-model` ของ Lead ตอน overload/not-found (default `claude-sonnet-5`, `""` = ปิด) |
| `TAKKUB_ALLOW_TASK=1` | un-block built-in `Task` tool ให้ panes (default block, ใช้ `takkub assign` แทน) |

ตั้ง env vars **ก่อน** launch cockpit จาก terminal เดียวกัน, หรือใส่ใน Windows system env vars

---

## Troubleshooting

> **เริ่มจาก `takkub doctor` เสมอ** — เช็ค claude / node / plugins / mcps / projects ครบในคำสั่งเดียว แล้ว print fix hint ต่อท้าย finding ที่ ⚠/✗ ส่ง `--fix` เพื่อ auto-regenerate `shared-mcp.json` ที่หาย

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
| `gemini` pane รันเป็น Claude (ไม่ใช่ Gemini จริง) | ยังไม่ได้ลง Antigravity `agy` หรือ ยังไม่ได้ sign-in — ลงที่ <https://antigravity.google/download> แล้วรัน `agy` 1 ครั้ง · เช็ค: `takkub doctor` (ดู providers) · ถ้าลงแล้วแต่ `agy` ไม่อยู่บน PATH → cockpit หาเจอเองที่ `%LOCALAPPDATA%\agy\bin` |
| `codex` pane รันเป็น Claude | ยังไม่ได้ลง/login Codex — `npm install -g @openai/codex` + `codex login` |

---

## Project structure

```
agent-takkub/
├── README.md                     # ไฟล์นี้
├── CLAUDE.md                     # Lead's system prompt (spawn-time)
├── projects.json                 # active project + paths + presets
├── pyproject.toml                # PyQt6 + dependencies
├── agent-takkub.bat              # one-click launcher (setup + launch)
├── scripts/install.ps1           # one-shot dependency installer — Windows (+ install.bat wrapper)
├── scripts/install.sh            # one-shot dependency installer — macOS/Linux (+ install.command wrapper)
├── .claude/agents/               # per-role agent definitions
├── bin/takkub  /  bin/takkub.cmd # POSIX + Windows CLI shims → agent_takkub.cli
├── runtime/                      # gitignored — port file, events.log, sessions, snapshots
├── src/agent_takkub/
│   ├── app.py                    # PyQt entry point (QtWebEngine flags, signal handlers)
│   ├── main_window.py            # QTabWidget, status bar, Finish Job
│   ├── project_tab.py            # per-tab Lead + teammate stack
│   ├── agent_pane.py             # pane header + state + terminal stack
│   ├── terminal_widget.py        # xterm.js terminal in QWebEngineView
│   ├── pty_session.py            # pywinpty wrapper + reader/writer threads
│   ├── orchestrator.py           # spawn/assign/send/close/done + auto-features
│   ├── lead_context.py           # Lead spawn prompt + plugin/MCP/env assembly
│   ├── pane_env.py               # per-pane env build (allowlist, ECC mute, MCP timeout)
│   ├── routing_planner.py        # auto-routing rules (classify → RoutingAction)
│   ├── cli_server.py / cli.py    # QTcpServer + `takkub` CLI client
│   ├── doctor.py                 # `takkub doctor` env diagnostics
│   ├── shared_dev_tools.py       # MCP shared config — browser + user-level MCP inheritance
│   ├── config.py / roles.py      # projects.json helpers + default role registry
│   ├── token_meter.py            # JSONL session reader + context budget
│   ├── vault_mirror.py           # Obsidian decision-log / digest / brief mirror
│   ├── issues.py                 # cockpit issue queue (takkub issue …)
│   └── … (provider config/dialogs, update workers, chatlog scanner, helpers)
└── tests/                        # ~1100 unit tests (pytest), no GUI required
```

---

## License

MIT — ดู `LICENSE`

---

## Pre-commit hooks

Repo มี hooks บังคับก่อน commit:

| Hook | ทำอะไร |
|---|---|
| `ruff` | lint + auto-fix |
| `ruff-format` | format ให้ตรง `pyproject.toml` |
| `takkub-docs-verify` | ตรวจ markdown refs ใน `docs/` + `CLAUDE.md` + `README.md` — block ถ้ามี drift |

ติดตั้งครั้งแรก:

```bash
pip install pre-commit
pre-commit install
```

หลังจากนั้น hook จะรันอัตโนมัติทุก `git commit`

ถ้า `takkub-docs-verify` fail ให้รัน `takkub docs-verify` เพื่อดู drift report (`runtime/docs_drift.md`)

---

## Contributing / Bug reports

GitHub Issues: <https://github.com/takkub/agent-takkub/issues>

PR welcome, ลอง `python -m pytest tests/` ก่อน push (ต้องผ่านครบทุก test)
