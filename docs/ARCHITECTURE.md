# Architecture

How agent-takkub is wired together. Read this when you're about to modify spawn semantics, IPC, or rendering.

## Process layout

```
                ┌─────────────────────────────────────────────┐
                │ agent-takkub cockpit (pythonw.exe, 1 proc)  │
                │ ┌─────────────────────────────────────────┐ │
                │ │ Qt main thread                          │ │
                │ │   MainWindow                            │ │
                │ │   ├── AgentPane(Lead)                   │ │
                │ │   ├── AgentPane(frontend)               │ │
                │ │   └── …                                 │ │
                │ │   Orchestrator                          │ │
                │ │   CliServer (QTcpServer, 127.0.0.1)     │ │
                │ │   LogsPanel (tails runtime/events.log)  │ │
                │ └─────────────────────────────────────────┘ │
                │ ┌─────────────────────────────────────────┐ │
                │ │ Reader QThreads (one per PtySession)    │ │
                │ │   block on winpty.read() → emit bytes   │ │
                │ └─────────────────────────────────────────┘ │
                └────┬──────────────┬───────────────┬─────────┘
                     │              │               │
                     ▼              ▼               ▼
           ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
           │ winpty-agent│  │ winpty-agent│  │ winpty-agent│
           │ + claude.exe│  │ + claude.exe│  │ + claude.exe│
           │ (Lead)      │  │ (frontend)  │  │ (backend)   │
           └─────────────┘  └─────────────┘  └─────────────┘
                              ▲
                              │ takkub CLI subprocess
                              │ (each agent runs this when it
                              │  wants to send / done / etc.)
                              ▼
                     ┌──────────────────┐
                     │ python -m        │
                     │  agent_takkub.cli│
                     │ → JSON over TCP  │
                     │ → CliServer      │
                     │ → Orchestrator   │
                     └──────────────────┘
```

## Module map

Complete map (40 modules). Grouped by concern; sizes change but the
groupings are stable. If you add a module, slot it here.

```
src/agent_takkub/
│ ── entry / app shell ──
├── __main__.py        so `python -m agent_takkub` works
├── app.py             QApplication entry point, signal handlers
├── main_window.py     top-level window: QTabWidget of ProjectTabs, status bar, dock, F1 help
├── project_tab.py     one project's pane stack (own Lead + teammate splitter) inside a tab
│ ── pane rendering ──
├── agent_pane.py      one grid slot: header chrome + state machine + terminal stack
├── terminal_widget.py QWebEngineView hosting xterm.js (static/terminal.html); raw PTY bytes
│                      → termWrite. Clickable URLs + file paths via WebLinksAddon + a custom
│                      link provider → QDesktopServices.openUrl
├── pty_session.py     pywinpty (WinPTY) + reader thread + pyte screen used ONLY for state
│                      detection (idle ❯ prompt, usage-limit banner) — NOT for rendering
├── _win_console.py    hide the stray ConsoleWindowClass HWND WinPTY leaks
│   static/            xterm.js + addons (fit, web-links) + terminal.html (renderer)
│ ── orchestration core ──
├── orchestrator.py    THE HEART: spawn/assign/send/close/done + auto-chain, watchdogs,
│                      rate-limit, resume briefs, snapshot/restore, broadcasts
├── cli_server.py      QTcpServer on main thread — parses JSON, dispatches to orchestrator
├── cli.py             `takkub` CLI client — newline JSON over TCP to runtime/port
├── routing_planner.py classify(msg,ctx) → RoutingAction; CLAUDE.md auto-routing as tested code
│                      (kinds incl. EXPLAIN_SYSTEM: "review/explain the system" → HTML explainer)
├── roles.py           role registry (3-col grid: lead + frontend/backend/mobile/devops/codex
│                      + gemini/qa/reviewer/critic/shell), colors, positions
├── config.py          projects.json + runtime/ paths + claude.exe finder
│ ── spawn-time context & env ──
├── lead_context.py    builds runtime/lead-context.md (CLAUDE.md + BLOCKED_DIRS + disabled-
│                      providers + brief), write-guard json, _SAFE_PLUGINS discovery
├── pane_env.py        per-pane env allowlist (drop secrets), ECC mute, MCP_TOOL_TIMEOUT inject
├── shared_dev_tools.py dev-tool config that follows Lead into every project tab
├── codex_agents_md.py auto-plant AGENTS.md into codex pane cwd
├── gemini_md.py       auto-plant GEMINI.md into gemini pane cwd
│ ── providers / plan ──
├── provider_config.py per-role CLI mapping (which CLI backs role X) — ~/.takkub/role-providers.json
├── provider_state.py  per-provider enable/disable state — ~/.takkub/disabled-providers.json
├── provider_dialog.py Qt dialog to pick claude/codex per role
├── plan_tier.py       account plan tier (Pro vs Max) — gates which models
├── codex_helper.py    OpenAI Codex CLI one-shot wrapper (non-interactive)
├── gemini_helper.py   Google Gemini CLI one-shot wrapper (mirror of codex_helper)
│ ── auth ──
├── claude_auth_config.py  optional Claude Code auth override (default = CC's own login)
├── claude_auth_dialog.py  Qt dialog for the auth override
│ ── observability / persistence ──
├── logs_panel.py      bottom dock tailing runtime/events.log
├── token_meter.py     per-pane context occupancy from claude session JSONL usage
├── issues.py          cockpit issue tracker — GitHub Issues backend via `gh` CLI
├── vault_mirror.py    Obsidian write-side: mirror `takkub done` notes/briefs into the vault
├── design_review_html.py  render critic's review .md → self-contained .html (screenshots
│                      inlined base64, impact→badge cards); `python -m …` CLI, critic runs it
├── chatlog_scanner.py read-only scan of CC per-project session JSONL (resume-brief source)
├── lead_bash_audit.py detect write-y Lead shell commands → JSONL audit record
│ ── diagnostics / verify ──
├── doctor.py          `takkub doctor` — pure-logic env diagnosis (no TCP, no network)
├── skill_audit.py     TF-IDF role-boundary audit — detect overlapping role responsibilities
├── verify.py          `takkub verify` — auto-detect stack + run lint/test gate
├── docs_verify.py     markdown reference verifier — catch stale file/symbol refs
│ ── self-update ──
├── rtk_helper.py      one-click install of rtk's PreToolUse Bash hook
├── update_helper.py   git-wrapper behind the status-bar update button
└── update_worker.py   QRunnable that fetches origin/main + reports local_status()
```

> **Note on layering:** the process-layout diagram above simplifies
> `MainWindow → AgentPane`. The real chain is `MainWindow → QTabWidget →
> ProjectTab → AgentPane` (one tab per project, each owning its own Lead).

## Data flow — "user types into Lead"

1. Keystroke lands inside **xterm.js** (the focused `QWebEngineView`); xterm encodes it to terminal bytes (arrow → `\x1b[A`, Thai → UTF-8).
2. xterm's `term.onData(data)` → `bridge.sendInput(data)` over QWebChannel → `_Bridge.inputData` signal → `TerminalWidget._on_input_data` emits `inputBytes(bytes)`.
3. `AgentPane` re-emits `inputBytes(role_name, data)`.
4. `Orchestrator._on_pane_input` writes the data to that pane's `PtySession`.
5. `PtySession.write(data)` decodes to `str` (pywinpty 3.x quirk) and calls `proc.write()`.
6. claude.exe receives the bytes via the WinPTY agent.

## Data flow — "claude prints something"

1. Reader QThread (one per session) is blocked on `proc.read(4096)`.
2. pywinpty returns the bytes / str — thread emits `bytesIn` to the main thread.
3. Two consumers run in parallel off `bytesIn`:
   - **render**: `TerminalWidget.write_bytes` batches per event-loop tick → `page.runJavaScript("termWrite(...)")` → xterm.js renders (the browser layout engine owns ANSI/alt-screen/IME/BiDi).
   - **state**: `PtySession` also feeds the bytes into a `pyte.Screen` purely to detect screen state (idle `❯` prompt, usage-limit banner). This drives `outputUpdated` / idle flags — it is **not** the render path.
4. There is no pyte→QTextCharFormat rebuild anymore (that was the pre-xterm pipeline); pyte is a headless state model now.

## Data flow — "user clicks a URL or file path in a pane"

1. xterm.js's `WebLinksAddon` (URLs) or the custom link provider in `terminal.html` (file paths) detects the span and underlines it on hover.
2. On click → `bridge.openUrl(uri)` / `bridge.openPath(path)` over QWebChannel.
3. `TerminalWidget._on_open_url` validates the scheme and calls `QDesktopServices.openUrl` (real OS browser — `window.open` is blocked inside QtWebEngine).
4. `TerminalWidget._on_open_path` resolves the token via `_resolve_open_path(raw, self._cwd, (REPO_ROOT,))` — absolute paths checked directly, relative paths against the pane cwd then the repo root — then `QDesktopServices.openUrl(QUrl.fromLocalFile(...))` so the OS default app opens it (html→browser, md→editor).

## Data flow — "Lead runs `takkub assign --role backend ...`"

1. Lead's claude executes a `Bash` tool call.
2. Bash spawns `bin/takkub.cmd` (the `bin/` dir was prepended to `PATH` when the Lead session was spawned).
3. `takkub.cmd` calls `.venv/Scripts/python.exe -m agent_takkub.cli assign --role backend ...`.
4. `cli.py` reads `runtime/port`, opens a TCP socket to `127.0.0.1:<port>`, sends one line of JSON.
5. `CliServer._on_ready_read` parses the JSON and dispatches to `Orchestrator.assign(role_name, cwd, task)`.
6. `Orchestrator.spawn(...)` emits `paneRequested("backend")` if no pane exists yet.
7. `MainWindow._ensure_teammate_pane` creates an `AgentPane`, registers it with the orchestrator, and adds it to the right-side vertical splitter.
8. `Orchestrator.spawn` resolves cwd (role-aware default), starts `claude.exe` via `pywinpty.PtyProcess.spawn` with:
   - `--dangerously-skip-permissions`
   - `--setting-sources <user,project,local>` (configurable)
   - `--append-system-prompt-file runtime/agents/backend/CLAUDE.md`
   - `--continue` if a recent matching exit is recorded
9. `Orchestrator._auto_trust(role_name)` schedules a 500ms-poll loop watching for the "trust this folder" modal and presses Enter when seen.
10. `Orchestrator._send_when_ready(role_name, task)` schedules another 500ms-poll loop watching for the idle `❯` prompt; when seen, writes the task + `\r`.
11. The pane's state goes `empty → active → working`.

## Lifecycle states (AgentPane)

```
   ┌──────┐  Spawn      ┌──────┐  task delivered   ┌──────┐
   │empty │ ──────────► │active│ ───────────────►  │working│
   └──────┘             └──────┘                   └──────┘
      ▲                    │                         │
      │                    │  unexpected exit        │  takkub done
      │                    ▼                         ▼
      │                ┌──────┐                    ┌──────┐
      └────────────────│exited│                    │ done │
        Spawn (=Respawn)└──────┘  2.5s grace      └──────┘
                                       │
                                       ▼
                                   close → pane removed from layout
```

The `_expected_exit` flag on `AgentPane` is set by `Orchestrator.close()` / `done()` so the next `processExited` signal doesn't surface as a crash.

## Concurrency model

- **Qt main thread** owns every widget, signal slot, `QTimer`, and IPC dispatch.
- **Reader QThread (per session)** is the only background thread. It does nothing but `proc.read()` + emit. Cross-thread signal emission is automatic (queued connection).
- No locks anywhere because (a) the orchestrator only runs on the main thread, and (b) `QTcpServer` queues `newConnection` events into the main thread's event loop.

## IPC schema (CLI ↔ orchestrator, newline-delimited JSON over localhost TCP)

```jsonc
// requests
{"cmd": "list"}
{"cmd": "spawn",     "role": "frontend", "cwd": "C:/path"}
{"cmd": "assign",    "role": "frontend", "cwd": "C:/path", "task": "..."}
{"cmd": "send",      "to":   "backend",  "msg": "...",     "from": "frontend"}
{"cmd": "close",     "role": "frontend"}
{"cmd": "close-all"}
{"cmd": "done",      "from": "frontend", "note": "..."}

// responses
{"ok": true,  "msg": "..."}
{"ok": true,  "msg": "status", "status": {"lead": "active", "frontend": "working"}}
{"ok": false, "msg": "<error>"}
```

The `from` field on `send` / `done` is populated by the CLI from the `TAKKUB_ROLE` env var that the orchestrator injects when spawning that pane.

## Persistence surface

| What | Where | Format |
|---|---|---|
| Active project / paths / presets | `projects.json` (repo root) | JSON |
| Audit log | `runtime/events.log` | JSONL (append-only) |
| TCP port number | `runtime/port` | plain int |
| Role staging CLAUDE.md | `runtime/agents/<role>/CLAUDE.md` | markdown (rewritten on each spawn from `.claude/agents/<role>.md`) |
| Pane buffer exports | `runtime/exports/<role>-<ts>.txt` | plain text |
| Window geometry + splitter | Windows registry via `QSettings("agent-takkub", "cockpit")` | binary |
| Per-role font size | Same QSettings, key `pane/<role>/font_pt` | int |

## Why some choices look weird

### Why pywinpty WinPTY (not ConPTY) backend?
ConPTY surfaces a visible conhost console window when spawned from a GUI process. WinPTY uses a hidden agent process. We tried both; WinPTY still leaks a `ConsoleWindowClass` HWND on some Windows builds, so we additionally diff-and-hide via `_win_console.py` for belt-and-suspenders.

### Why `--append-system-prompt-file`, not inline text?
Role markdown contains backticks, asterisks, and Thai diacritics. Inline-string `--append-system-prompt "<long text>"` runs into Windows argv quoting limits and CommandLineToArgvW edge cases. The file variant avoids both.

### Why does `Orchestrator.spawn` emit `paneRequested` instead of creating panes itself?
Pane widgets are Qt UI objects — only `MainWindow` knows about the splitter layout. Keeping pane construction in `main_window.py` and orchestration in `orchestrator.py` lets the orchestrator stay UI-agnostic (e.g. easier to unit-test).

### Why a TCP server inside a GUI process?
`QTcpServer` lives on the Qt main thread and emits `newConnection` as a normal Qt signal. That means every IPC call from `takkub` CLI is serialised through the same event loop as UI events — no lock juggling, no race conditions, no GIL surprises.

### Why ship a `.venv` launcher (`run.bat`) instead of pip installing as a script?
PyQt6 wheels are large and `pywinpty` requires a Windows build toolchain to compile from source. Forcing a `.venv` lets users on locked-down machines run without poking system Python. The `start "" pythonw.exe` trick lets the launcher batch exit immediately so no cmd.exe lingers.

## Test surface

Unit tests cover the non-Qt parts:

- `tests/test_config.py` — projects.json loader, role-aware cwd, presets, port file roundtrip
- `tests/test_roles.py` — default registry, by_name (case-insensitive)
- `tests/test_cli.py` — argparse for every subcommand, exit codes, Thai bytes round-trip

The Qt + pywinpty parts are validated by live end-to-end runs against the real PMS project (see TASKS.md → "End-to-end verifications").

CI (`.github/workflows/ci.yml`) on `windows-latest`:
1. `pip install -e .[dev]`
2. `ruff check .` + `ruff format --check .`
3. Import smoke test on the non-Qt modules
4. `pytest -v`

Future test work (deferred): a `pytest-qt` harness for `AgentPane` state transitions and a mock `PtySession` for orchestrator integration tests.
