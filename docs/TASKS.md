# Tasks completed

A flat checklist of every task done while building agent-takkub from scratch, in order. Each row links back to the iteration where it was introduced and notes whether it was verified live.

> Legend: ✅ verified end-to-end · 🧪 unit-tested · 📝 code-ready, not exercised live · ⚪ deferred

---

## Iter 1 — baseline cockpit (initial commit)

| Status | Task |
|---|---|
| ✅ | Initialise Python project (`pyproject.toml`, src layout, `.venv`) |
| ✅ | Install dependencies: `PyQt6`, `pywinpty`, `pyte` |
| ✅ | `roles.py` — registry of 7 default teammates + LEAD |
| ✅ | `config.py` — projects.json loader, runtime dir helpers, claude.exe finder |
| ✅ | `pty_session.py` — pywinpty + pyte glue with QThread reader |
| ✅ | `terminal_widget.py` — QPlainTextEdit + key/IME input handlers |
| ✅ | `agent_pane.py` — header + placeholder ↔ active terminal stack |
| ✅ | `orchestrator.py` — high-level spawn/assign/send/close/done |
| ✅ | `cli_server.py` — QTcpServer for JSON IPC |
| ✅ | `cli.py` — `takkub` CLI client (spawn/assign/send/close/done/list) |
| ✅ | `main_window.py` — 3-column splitter (Lead · middle · right) |
| ✅ | `app.py` + `__main__.py` — Qt application entry |
| ✅ | `bin/takkub.cmd` — CLI shim resolving to .venv python |
| ✅ | `scripts/run.bat` — venv-on-first-run launcher |
| ✅ | Migrate 7 role `.md` files from agent-teams (tmux → takkub CLI) |
| ✅ | Write Lead `CLAUDE.md` for the new cockpit |

### Iter 1.5 — post-launch debugging (live fixes)

| Status | Task |
|---|---|
| ✅ | Hide ConsoleWindowClass HWNDs after spawn (`_win_console.py` diff before/after) |
| ✅ | `run.bat` switched to `pythonw.exe` + `start ""` so the launcher cmd.exe exits |
| ✅ | pywinpty `read(size)` signature fix — `num_bytes` kwarg was wrong |
| ✅ | pywinpty `write()` expects `str` not `bytes` — silent TypeError was eating keystrokes |
| ✅ | EOFError handling — check isalive() before treating empty read as termination |
| ✅ | Thai diacritic regression fix — preserve QFont families fallback chain inside QTextCharFormat |
| ✅ | Switch from default ConPTY backend to WinPTY backend in pywinpty.spawn |
| ✅ | `find_claude_executable()` prefers real `claude.exe` over `claude.cmd` wrapper |

## Iter 2 — interactivity polish

| Status | Task |
|---|---|
| ✅ | Auto-trust folder prompt (poll for "trust this folder" → send Enter) |
| ✅ | Auto-detect idle `❯` prompt before pasting `assign` task (replaces 12s fixed wait) |
| ✅ | Mouse wheel → PgUp/PgDn forwarding for claude alt-screen scroll |
| ✅ | Close fully removes pane from layout (was leaving empty placeholder) |
| ✅ | `orchestrator.paneClosed` signal → `main_window._remove_teammate_pane` |
| ✅ | Live verify with `takkub list/assign/send/close` chain |

## Iter 3 — colours + status

| Status | Task |
|---|---|
| ✅ | `PtySession.display_rich()` — per-cell color/attrs run packing |
| ✅ | `TerminalWidget` renders ANSI colours via QTextCharFormat cache (16-colour palette + truecolor) |
| ✅ | Spinner animation + elapsed-time counter on `working` panes (`◐◓◑◒` 250ms tick) |
| ✅ | Project switcher combo in status bar (writes back to projects.json) |
| ✅ | "+ pane" button with default-or-custom role picker |

## Iter 4 — persistence + role-awareness

| Status | Task |
|---|---|
| ✅ | Window geometry + main splitter sizes persisted via QSettings |
| ✅ | Role-aware default cwd resolution (`frontend→web`, `backend→api`, ...) |
| ✅ | `--append-system-prompt-file <role.md>` so specialist override applies even when cwd is the project root |
| ✅ | Event audit log at `runtime/events.log` (JSONL: spawn/assign/send/close/done) |
| ✅ | Cleaned redundant 2.7s close path in main_window |

## Iter 5 — crash recovery + chrome

| Status | Task |
|---|---|
| 📝 | Crash recovery: AgentPane.`_expected_exit` flag → unexpected exits show orange `exited` state with respawn button |
| ✅ | Spawn errors surfaced in status bar (`⚠ Lead spawn failed: ...`) |
| ✅ | Font-size shortcuts inside terminal: Ctrl+= / Ctrl+- / Ctrl+0 (reset) |
| ✅ | Lead pane shows active project name in header (`Lead · pms`) |
| ✅ | Verified `takkub done` end-to-end (done → 2.5s grace → close → pane removed) |

## Iter 6 — discoverability

| Status | Task |
|---|---|
| ✅ | Bottom dock `LogsPanel` — tails runtime/events.log every 1s |
| ✅ | F1 / `?` help dialog with takkub cheatsheet + shortcuts |
| ✅ | "⟶ assign" quick-assign button (role picker + multi-line task input) |
| ✅ | `takkub close-all` command — close every teammate, keep Lead |

## Iter 7 — resilience + UX persistence

| Status | Task |
|---|---|
| ✅ | Session resume: `claude --continue` on respawn within 5min same cwd (verified via events.log `resumed: true`) |
| ✅ | Desktop notification (`QSystemTrayIcon` toast) when an agent calls `takkub done` |
| ✅ | Export pane buffer to `.txt` via `⤓` button (`runtime/exports/<role>-<ts>.txt`) |
| ✅ | Per-role font size persisted in QSettings (key `pane/<role>/font_pt`) |

## Iter 8 — context awareness

| Status | Task |
|---|---|
| ✅ | Pane header shows cwd basename (`Frontend · pms-web`) |
| ✅ | Status bar live count: `2 active · 1 working` (QTimer 2s tick) |
| ✅ | Auto-spawn presets per project (`projects.json → presets: [...]`, staggered 15s+3s/role) |
| ✅ | Logs panel: filter by event type + role substring |

## Iter 9 — final polish

| Status | Task |
|---|---|
| ✅ | Pane minimise/restore toggle (`▾`/`▸` button) |
| ✅ | Logs panel text search (case-insensitive substring) |
| ✅ | Custom-role colour picker (`+ pane → custom...` → QColorDialog) |
| ✅ | README rewritten to cover all features |

## v0.1 release polish

| Status | Task |
|---|---|
| ✅ | `.gitattributes` enforcing LF on .sh/.py/.md and CRLF on .bat/.cmd/.ps1 |
| ✅ | `git init -b main` + initial commit `9936292` |
| ✅ | `ruff` configured (line 100, py311) — auto-formatted 16 files, fixed RUF005 + spurious `noqa`s |
| 🧪 | `tests/test_config.py` — 14 tests (active project, role-aware cwd, presets, port file) |
| 🧪 | `tests/test_roles.py` — 6 tests (default registry, by_name lookup) |
| 🧪 | `tests/test_cli.py` — 17 tests (argparse payloads, exit codes, Thai bytes round-trip) |
| ✅ | `CHANGELOG.md` documenting Iter 1-9 |
| ✅ | `.github/workflows/ci.yml` — windows-latest: install deps, ruff lint+format, smoke imports, pytest |
| ✅ | gh portable v2.92.0 downloaded → `gh auth login` (SSH, takkub account) |
| ✅ | `gh repo create takkub/agent-takkub --private` + push |
| ✅ | tag `v0.1.0` + `gh release create v0.1.0` |

## v0.2.0 (current)

| Status | Task |
|---|---|
| ✅ | Install `superpowers@superpowers-dev` v5.1.0 plugin (obra/superpowers) |
| ✅ | Default `--setting-sources` flipped to `user,project,local` → agents see installed plugins + MCP |
| ✅ | `TAKKUB_SETTING_SOURCES` env var override for isolated mode |
| ✅ | Orphan cleanup: `atexit` + SIGINT/SIGTERM/SIGBREAK handlers in `app.py` |
| ✅ | Lead `CLAUDE.md` gains takkub quick-reference + "Tooling available to agents" section |
| ✅ | tag `v0.2.0` + `gh release create v0.2.0` |

## End-to-end verifications (live runs)

| Date | Scope | Outcome |
|---|---|---|
| 2026-05-12 | Single-agent: frontend creates pms-web/app/agent-takkub-test/page.tsx | ✅ agent inspected project conventions (used antd instead of suggested shadcn), wrote file in ~50s |
| 2026-05-12 | Multi-agent: backend + frontend with peer-comm | ✅ backend made NestJS health controller + module, frontend waited for `takkub send` then implemented page, both auto-closed via `takkub done` |

Both events captured in `runtime/events.log`.

---

## Deferred (not in v0.1 / v0.2 — candidates for v0.3)

- ⚪ Screenshots / GIF demo in README (needs user to capture)
- ⚪ Multi-instance support (port file collision today, single cockpit only)
- ⚪ Light theme / theme toggle
- ⚪ Drag-to-reorder teammate panes
- ⚪ PTY resize fully verified (signal wired, never live-tested with claude reflow)
- ✅ Memory growth on long sessions (`_fmt_cache` no cap) — **obsolete**: the unbounded `_fmt_cache` dict was removed by the xterm.js migration; ANSI rendering now happens entirely in the browser layer (no Python-side per-attribute cache). No code change needed.
- ⚪ `install.sh` for macOS/Linux (Windows-first by design)
- ✅ **Backend done-without-commit protocol bug** — FIXED via opt-in `--requires-commit` flag (2026-05-21). pane รายงาน `takkub done` ครบทุก phase แต่ไม่ `git commit` ก่อน → Lead เห็น working tree มี modified files ค้าง ต้อง commit แทน. Fix: `takkub assign --role backend --requires-commit "task"` → orchestrator gate `takkub done` ด้วย `git status --porcelain`; dirty = reject + inject error message เข้า pane; clean = done ตามปกติ. Default=False (review-only tasks ไม่กระทบ). [commit `9bc04b8`]
- ⚪ **Codex done-protocol bug** — codex pane เขียน output ไฟล์ครบเรียบร้อยแล้ว แต่บางครั้งไม่เรียก `takkub done` ตัวเอง → ค้างอยู่ใน state `working` จน Lead ต้อง `takkub close --role codex` แบบ manual (เกิด 2026-05-21 ตอน review oh-my-agent-temp) ลองดู: (a) prompt instruction ของ codex teammate ใน .claude/agents/codex.md มีบอกชัดเจนเรื่อง done ไหม (b) codex CLI behavior ต่างจาก claude code ไหม (c) watchdog ใน orchestrator detect "task complete but pane idle" → auto-prompt "เรียก takkub done สิ" ได้หรือไม่
- ✅ **Resume bleed: teammate panes inherit Lead's chat history** — FIXED 2026-05-21 via option B: `--session-id <uuid>` at fresh spawn, `--resume <uuid>` within `RESUME_WINDOW_SEC`, no more `--continue`. Each pane gets an isolated session UUID stored in `orchestrator._session_uuids`; claude's CWD-based `--continue` resolution is bypassed entirely. UUID is cwd-locked per session (qa confirmed `--resume` is CWD-scoped, see `docs/reviews/2026-05-21-claude-resume-behavior.md`). `close()` and `done()` pop the UUID so the next spawn starts fresh. 8 new tests in `tests/test_orchestrator_session_uuid.py`. [commits `365419c` + `7624ba9`]
- 📝 **Codex CLI early-crash on Windows under cockpit env** — codex pane spawn ครั้งแรกตายเองภายใน ~50s ไม่มี error visible (transcript เหลือแค่ 2.6KB = ขึ้น banner แล้วเงียบ) เกิดซ้ำ 2026-05-21 ตอน review session. orchestrator auto-respawn สำเร็จในครั้งที่ 2 (ทำงานต่อได้) แต่ก่อน fix `auto_respawn_replay` (commit หลัง) task ที่ assign ครั้งแรกหายไปเลย → user เห็น dead pane. **Instrumentation added (2026-05-21):** `_on_codex_exit()` + `_write_codex_crash_dump()` ใน orchestrator — early crash (< 90s) → เขียน dump ไว้ที่ `runtime/codex_crash_dumps/<ts>-<project>-<role>.log` พร้อม exit_code, time_to_exit, last PTY output, env keys. Event `codex_early_crash` logged เพื่อ trace ใน events.log. 6 new tests in `tests/test_codex_crash_instrumentation.py`. Hypotheses + experiments ranked ใน `docs/reviews/2026-05-21-codex-crash-hypotheses.md` (gemini analysis): top 2 = (a) MCP boot race (score 14) (b) env allowlist missing `COMSPEC` (score 14). **Next step (manual):** repro crash → read dump → run Experiment 2 (env bypass) first, then Experiment 1 (delay paste).
- 📝 **Lead hardening — Gap A: Bash write-boundary bypass** — Lead-side `Edit`/`Write` tools deny BLOCKED_DIRS, but shell (`python -c "open(...).write(...)"`, `Set-Content`, redirect `>`, `git apply`) goes through unrestricted. Phase 1 audit-only implementation in `src/agent_takkub/lead_bash_audit.py`: `detect_write_intent(cmd)` + `audit_lead_bash(cmd, cwd)` → appends JSONL to `runtime/lead_bash_audit.log`. Wire via Claude Code PreToolUse hook on `Bash` tool (see docstring in module). Phase 2 (blocking/deny) not yet implemented. (Surfaced by codex review during Lead self-protection fix, 2026-05-20.)
- ✅ **Lead hardening — Gap B: CliServer role gate** — fixed in `cli_server.py:_dispatch` with two-layer guard (Layer 1: stamped `from` field must equal `"lead"`; Layer 2: `auth` token via `secrets.compare_digest` against `_lead_token`). Lifecycle commands (spawn/assign/close/close-all) rejected when either gate fails. `cli.py` stamps `from: _from_role()` automatically on outbound payloads. 13 new tests in `tests/test_cli_server_role_gate.py`. (Resolved 2026-05-21.)
