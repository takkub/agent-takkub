# agent-takkub

Desktop cockpit for orchestrating Claude Code dev teammates on Windows. Replaces the tmux-based `agent-teams` setup with a PyQt6 GUI, native Windows PTY (pywinpty), and a small CLI (`takkub`) that agents use to talk to each other.

> **Companion docs:** [`docs/TASKS.md`](docs/TASKS.md) (build log) · [`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md) (constraints & out-of-scope) · [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (how it works) · [`CHANGELOG.md`](CHANGELOG.md) (version history)

## ทำไมเปลี่ยน

`agent-teams` เดิมพึ่ง tmux ที่ทำงานไม่ดีบน Windows native: MSYS tmux server ตายเองตอน parent shell exit, winpty wrapping ปัญหา, CRLF, ฟอนต์ไทยใน mintty ต้องตั้งเอง. `agent-takkub` จัดการทั้งหมดในแอป Qt ตัวเดียว.

- Windows ConPTY/WinPTY ผ่าน pywinpty
- GUI desktop app เปิดจาก Start menu / shortcut
- พิมพ์ไทย + IME + diacritics (สระ, tone marks) render ครบ
- Lazy spawn — เปิด pane เมื่อ Lead สั่ง, ปิดอัตโนมัติเมื่อ agent ทำ `takkub done`
- Auto-trust folder prompt
- Auto-detect ❯ ready prompt ก่อน paste task (ไม่มี fixed delay)
- Session resume (`claude --continue`) เมื่อ respawn ภายใน 5 นาทีในที่ cwd เดิม
- Desktop toast notification เมื่อ agent done
- Event audit log (`runtime/events.log`)

## Requirements

- Windows 10/11
- Python 3.11+
- Claude Code CLI ติดตั้งและล็อกอินแล้ว (`claude --version` ต้อง work)

## Setup

```bat
cd C:\Users\monch\WebstormProjects\agent-takkub
scripts\run.bat
```

ครั้งแรกจะสร้าง `.venv\` และ `pip install -e .` (PyQt6 + pywinpty + pyte) แล้วเปิดแอป. ครั้งถัดไปแค่รัน `scripts\run.bat` ซ้ำ.

## Layout

```
┌────────────────────────────────┬──────────────────────────┐
│ ● Lead · pms                ×  │ ● Frontend · pms-web  × │
│                                │                          │
│  <claude TUI for Lead>         │  <claude TUI>            │
│                                │                          │
│                                ├──────────────────────────┤
│                                │ ● Backend · pms-api   × │
│                                │  <claude TUI>            │
│                                │                          │
└────────────────────────────────┴──────────────────────────┘
status: cockpit · cli 51609 · 2 active · 1 working     [project: pms ▼] [+ pane] [⟶ assign] [logs] [?]
```

- Lead pane เต็มจอตอนเริ่ม
- `takkub assign --role X "task"` → ขวา split ลงล่างตามจำนวน agents
- Agent `takkub done` → pane เด้งหายอัตโนมัติ + Lead รับแจ้ง

## `takkub` CLI (รันจาก pane ไหนก็ได้)

```bash
takkub list
takkub spawn --role <role> [--cwd <path>]
takkub assign --role <role> [--cwd <path>] "<task>"
takkub send --to <role> "<msg>"        # peer message, CCs Lead
takkub close --role <role>
takkub close-all                       # close all teammates, keep Lead
takkub done [note]                     # agents call this when finished
```

ถ้า `--cwd` ไม่ระบุ orchestrator เลือก path จาก active project อัตโนมัติ:

| Role | Default cwd preference |
|---|---|
| frontend, designer | `web` → `client` → `frontend` |
| backend | `api` → `server` → `backend` |
| mobile | `mobile` → `app` → `web` |
| devops | `api` → `infra` → `ci` → `ops` |
| qa | `web` → `api` |
| reviewer | `api` → `web` |
| custom | first path in active project |

## projects.json

```json
{
  "active": "pms",
  "projects": {
    "pms": {
      "description": "PMS",
      "paths": {
        "web": "C:/Users/monch/WebstormProjects/pms/pms-web",
        "api": "C:/Users/monch/WebstormProjects/pms/pms-api"
      },
      "presets": ["frontend", "backend"]
    }
  }
}
```

- `paths` — มาตรฐาน key (web/api/mobile/infra ฯลฯ); ใช้สำหรับ role-aware cwd
- `presets` — รายชื่อ role ที่จะ auto-spawn 15s หลัง Lead boot (ตั้งระยะ 3s ต่อ role)

Switch active project ผ่าน combo box ใน status bar.

## Keyboard / mouse shortcuts

- **F1** — help cheatsheet
- **Ctrl + + / - / 0** ใน terminal — ขนาดฟอนต์ (จำต่อ pane)
- **Mouse wheel** ใน terminal — forward เป็น PgUp/PgDn ให้ claude scroll history เอง
- **กดปุ่ม ×** ใน pane header — ปิด pane (เหมือน `takkub close --role X`)
- **กดปุ่ม ▾ / ▸** ใน pane header — minimise/restore (collapse body, header strip)
- **กดปุ่ม ⤓** ใน active pane — export pane buffer เป็น `.txt` ใน `runtime/exports/`

## Status bar widgets (ขวา → ซ้าย)

| Widget | ทำอะไร |
|---|---|
| `?` | help dialog (F1) |
| `logs` | toggle bottom panel แสดง events.log live |
| `⟶ assign` | quick-assign dialog: pick role + multi-line task |
| `+ pane` | dialog เพิ่ม pane จาก default role หรือ "custom..." (พร้อม color picker) |
| `project: ▼` | combo box สลับ active project |

## Auto-trust + session resume

- ตอน claude แสดง trust folder modal — orchestrator detect แล้วส่ง Enter อัตโนมัติ (poll ทุก 500ms, max 30s)
- ตอน claude ready (`❯` idle prompt) — orchestrator paste task ที่ queue ไว้ (poll, fallback 45s timeout)
- ถ้า claude.exe ตาย unexpected — pane state เปลี่ยนเป็น 🟠 `exited` + note exit code, คลิก Spawn เพื่อ respawn
- Respawn ภายใน 5 นาทีใน cwd เดิม → ผ่าน `--continue` ให้ claude resume conversation ก่อนหน้า

## Specialist role override

แต่ละ teammate pane spawn ด้วย `--append-system-prompt-file .claude/agents/<role>.md` เพื่อบังคับ specialist behavior แม้ cwd เป็น project root ที่มี CLAUDE.md ของ Lead.

ใน `runtime/agents/<role>/CLAUDE.md` คือ specialist instructions + ตัวอย่างคำสั่ง `takkub send` / `takkub done`.

## Files generated at runtime

| Path | Purpose |
|---|---|
| `runtime/port` | TCP port ที่ cli_server bind (เพื่อ `takkub` CLI เชื่อม) |
| `runtime/events.log` | JSONL audit trail ของ spawn / assign / send / close / done |
| `runtime/agents/<role>/CLAUDE.md` | specialist role override (copy จาก `.claude/agents/`) |
| `runtime/exports/<role>-<ts>.txt` | manual buffer exports จากปุ่ม ⤓ |

`runtime/` ถูก gitignore.

## Project structure

```
agent-takkub/
├── CLAUDE.md                  # Lead instructions
├── projects.json              # active project + paths + presets
├── pyproject.toml
├── .claude/agents/            # 7 default role CLAUDE.md (frontend/backend/...)
├── src/agent_takkub/
│   ├── app.py                 # PyQt entry
│   ├── main_window.py         # 3-column dynamic splitter, status bar
│   ├── agent_pane.py          # header + state + terminal stack
│   ├── terminal_widget.py     # pyte rendering + ANSI colors + key/IME/wheel
│   ├── pty_session.py         # pywinpty + pyte glue
│   ├── orchestrator.py        # spawn/assign/send/close/done + auto-trust + resume
│   ├── cli_server.py          # QTcpServer for takkub CLI
│   ├── cli.py                 # `takkub` CLI client
│   ├── logs_panel.py          # bottom dock: tail events.log + filter/search
│   ├── _win_console.py        # hide ConsoleWindowClass HWNDs after spawn
│   ├── config.py              # projects.json + runtime/ helpers
│   └── roles.py               # default role registry
├── bin/takkub.cmd             # CLI shim → .venv\python -m agent_takkub.cli
└── scripts/run.bat            # launcher (pythonw, console-less)
```

## Known limits

- Thai chars proportional inside terminal (no Thai monospace ships on Windows). Latin remains mono via Cascadia.
- Claude alt-screen buffer is not in pyte's history — use mouse wheel to scroll inside claude's own UI.
- Single-instance only (port file is global). Run separate Python user account if you need parallel cockpits.
- Custom role color picked via "+ pane → custom..." resets between cockpit restarts (in-memory only).
