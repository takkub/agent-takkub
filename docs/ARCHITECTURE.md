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

```
src/agent_takkub/
├── app.py            QApplication setup, signal handlers, MainWindow.show()
├── __main__.py       so `python -m agent_takkub` works
├── main_window.py    layout, project switcher, status bar, dock, F1 help
├── agent_pane.py     header chrome + state machine + terminal stack
├── terminal_widget.py  QPlainTextEdit subclass: pyte → QTextCharFormat
├── pty_session.py    pywinpty + pyte + reader thread + screen-state helpers
├── orchestrator.py   spawn / assign / send / close / done semantics
├── cli_server.py     QTcpServer dispatching JSON to orchestrator
├── cli.py            `takkub` CLI client — TCP JSON to runtime/port
├── logs_panel.py     bottom dock tailing runtime/events.log
├── roles.py          default role registry (lead + 7 teammates)
├── config.py         projects.json + runtime/ + claude.exe finder
└── _win_console.py   ConsoleWindowClass HWND hide helper
```

## Data flow — "user types into Lead"

1. User keystroke lands on `TerminalWidget` (focused).
2. `keyPressEvent` translates the key to PTY bytes (e.g. arrow → `\x1b[A`, Thai char → UTF-8) and emits `inputBytes`.
3. `AgentPane` re-emits `inputBytes(role_name, data)`.
4. `Orchestrator._on_pane_input` writes the data to that pane's `PtySession`.
5. `PtySession.write(data)` decodes to `str` (pywinpty 3.x quirk) and calls `proc.write()`.
6. claude.exe receives the bytes via the WinPTY agent.

## Data flow — "claude prints something"

1. Reader QThread (one per session) is blocked on `proc.read(4096)`.
2. pywinpty returns the bytes / str — thread `emit(bytesReceived)` to main thread.
3. `PtySession._on_bytes` feeds the bytes into `pyte.ByteStream` which updates the `pyte.Screen` model.
4. `PtySession.outputUpdated` signal fires.
5. `AgentPane._refresh_terminal` calls `session.display_rich()` → list of styled rows.
6. `TerminalWidget.set_screen_rich(rows)` schedules a debounced redraw (16ms).
7. `_flush_rich` clears the QTextDocument and inserts each run with a cached `QTextCharFormat`. The cache is keyed by `(fg, bg, bold, italic, underline, reverse)`.

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
