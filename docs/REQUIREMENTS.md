# Requirements & constraints

What agent-takkub does, what it doesn't, and what it needs from its host.

## Purpose

A single-window desktop cockpit that lets one human (the Lead's user) drive a small team of specialised Claude Code agents on one Windows machine, replacing the tmux-based [agent-teams](https://github.com/itseed/agent-teams) workflow with native Windows tooling.

The Lead pane is a regular Claude Code session that knows how to dispatch work via the `takkub` CLI. Each teammate is its own Claude Code session running in a separate PTY, with its own conversation history, isolated cwd, and an injected specialist role override.

---

## Functional requirements

1. **Run one Lead pane on startup** in the cockpit's own repo (so Lead's CLAUDE.md is auto-discovered).
2. **Spawn / close teammate panes on demand** via either the `takkub` CLI or the `+ pane` / `⟶ assign` buttons.
3. **Route messages** between panes:
   - `takkub assign --role X "task"` — spawn (if needed) and deliver a task once claude is ready (`❯` prompt).
   - `takkub send --to Y "msg"` — write to Y's PTY, CC Lead automatically when sender isn't Lead.
   - `takkub done [note]` — record completion, notify Lead, auto-close the pane after a 2.5s grace.
4. **Render claude's TUI faithfully** — ANSI colours, bold/italic/underline, Thai diacritics, mouse-wheel scroll, IME composition.
5. **Survive everyday failure modes**:
   - claude crashes → pane shows orange `exited`, user can respawn.
   - quick respawn in the same cwd → `--continue` to resume the previous conversation.
   - cockpit crashes / SIGINT / SIGTERM / SIGBREAK → orphan claude/winpty-agent processes are killed.
6. **Persist state across launches**:
   - Window geometry + main splitter sizes
   - Per-role font size
   - Active project selection (write-through to projects.json)
7. **Provide an audit trail** at `runtime/events.log` of every spawn/assign/send/close/done with ISO timestamps + cwd + previews.

## Non-functional requirements

| Aspect | Requirement |
|---|---|
| OS | Windows 10 (build 19041+) or Windows 11 |
| Python | 3.11 or later |
| Claude CLI | Installed, authenticated (Claude Max / OAuth) |
| Startup time | Lead pane ready within ~10s on a typical dev machine |
| First-run install | `scripts\run.bat` provisions `.venv` and installs deps in under 2 minutes |
| Repeat launch | < 5s from `run.bat` invocation to Qt window visible |
| Memory budget | ≤ 500 MB resident for the cockpit process alone (each spawned claude/winpty-agent adds ~150-300 MB) |
| Per-frame redraw | ≤ 16 ms (60 fps debounce in `TerminalWidget._flush_rich`) |
| Audit log size | Soft cap by user; no auto-rotation; log is append-only JSONL |

## Hard dependencies

| Package | Version | Why |
|---|---|---|
| `PyQt6` | ≥ 6.6 | Window + widgets + QTcpServer + QSettings + signals |
| `pywinpty` (`winpty`) | ≥ 2.0 (tested 3.0.3) | Windows ConPTY/WinPTY backend |
| `pyte` | ≥ 0.8 | ANSI/VT100 screen model |
| `wcwidth` | (transitive via pyte) | East Asian width tables for cell sizing |

Dev-only (CI + tests):

| Package | Version | Why |
|---|---|---|
| `pytest` | ≥ 8 | Test runner |
| `ruff` | ≥ 0.7 | Linter + formatter |

External tools needed at runtime (not bundled):

- `claude` CLI on PATH (preferred: the real `claude.exe` inside `node_modules/@anthropic-ai/claude-code/bin/`)
- Standard Windows console subsystem (the cockpit hides spawned console HWNDs but the subsystem must exist)

## Constraints

### Single instance per machine
`runtime/port` is a global path. Two cockpit processes will fight over it. If you need to drive two projects in parallel, run the second cockpit under a different Windows user account today. A future v0.3 may move the port file under `%APPDATA%\agent-takkub\<instance-id>\port`.

### Windows-first by design
- Hidden-console logic uses `user32!EnumWindows` + `ShowWindow(SW_HIDE)` against `ConsoleWindowClass`. No equivalent path exists on Linux/macOS.
- PTY backend is `pywinpty` (WinPTY). Porting to mac/Linux would need `ptyprocess` or `pexpect`.
- Signal handlers cover `SIGINT/SIGTERM/SIGBREAK` (SIGBREAK is Windows-only).

### Plugins-on-by-default may surface upstream bugs
`v0.2` default `--setting-sources user,project,local` lets every spawned claude session inherit the user's installed Claude Code plugins (superpowers, agent-skills, claude-obsidian) and MCP servers. If a global plugin's SessionStart hook misbehaves, set `TAKKUB_SETTING_SOURCES=project,local` before launching the cockpit to fall back to the isolated v0.1 behaviour.

### Claude alt-screen vs pyte scrollback
Claude's TUI uses the terminal alt-screen buffer. `pyte.Screen` (not `HistoryScreen`) tracks only the visible viewport, so pyte's scrollback is effectively useless here. Mouse wheel is forwarded as `PgUp/PgDn` so claude itself handles scrollback inside the alt-screen.

### Thai monospace ships nowhere on Windows
The terminal uses a font fallback chain (`Cascadia Mono → Consolas → Courier New → Leelawadee UI → Tahoma → Microsoft Sans Serif`). Thai characters fall through to Tahoma/Leelawadee, which are proportional — so Thai text isn't strictly mono-aligned. Latin stays mono. A user who installs `Sarabun Mono` or `IBM Plex Mono Thai` gets full mono.

### Command-line length for role markdown
Role specialist markdown is passed via `--append-system-prompt-file <path>` rather than `--append-system-prompt <text>` so multi-KB markdown with backticks, asterisks, and Thai text doesn't run into Windows argv quoting issues.

### Trust-folder prompt is auto-accepted
The auto-trust poller sends `Enter` when claude shows "Yes, I trust this folder". This means the cockpit will trust any cwd a teammate is told to spawn into. Don't point teammate spawns at directories you wouldn't trust on your machine.

## Out of scope (won't be in v0.x)

- **Cross-machine orchestration** — single workstation only; no remote agents.
- **Authentication / multi-user** — the cockpit assumes one user, one Claude account, one machine.
- **A Lead model selector / per-pane model overrides** — every spawned claude uses whichever model the user's `claude` CLI defaults to (typically Opus via Claude Max).
- **Built-in collaboration log** beyond `runtime/events.log` — no chat history viewer, no diff timeline.
- **Replacement for Claude Code's own UI** — agent-takkub is a wrapper around the existing TUI, not a re-implementation.
- **Pretty REST API** — the IPC surface (JSON over localhost) is private to `takkub` CLI consumers; the schema may change without notice.

## Threat model assumptions

- All processes run as one Windows user; no privilege separation.
- TCP socket binds to `127.0.0.1` only — other machines on the LAN cannot reach it.
- Anything you can write into `runtime/port` you could already write into the user's home, so the port file is not a trust boundary.
- `takkub` CLI does **not** authenticate callers — any local process under the same user can talk to the orchestrator. This is by design (the CLI is the cockpit's own UI surface) and matches the trust model of any single-user developer tool.
