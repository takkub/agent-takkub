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

Complete map (45 modules). Grouped by concern. List generated from `ls src/agent_takkub/*.py`.

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
│                      rate-limit, resume briefs, snapshot/restore, broadcasts, shard coordination
├── cli_server.py      QTcpServer on main thread — parses JSON, dispatches to orchestrator
├── cli.py             `takkub` CLI client — newline JSON over TCP to runtime/port
├── routing_planner.py classify(msg,ctx) → RoutingAction; CLAUDE.md auto-routing as tested code
├── roles.py           role registry (3-col grid: lead + frontend/backend/mobile/devops/codex
│                      + gemini/qa/reviewer/critic/shell), colors, positions
├── config.py          projects.json + runtime/ paths + claude.exe finder
│ ── spawn-time context & env ──
├── lead_context.py    builds runtime/lead-context.md (CLAUDE.md + BLOCKED_DIRS + brief),
│                      write-guard json, _SAFE_PLUGINS discovery
├── project_rules.py   injects Lead's constraint registry (MEMORY.md) pointers into teammates
├── pane_env.py        per-pane env allowlist (drop secrets), ECC mute, MCP_TOOL_TIMEOUT inject
├── shared_dev_tools.py shared MCP config management + role-aware tool filtering
├── codex_agents_md.py auto-plant AGENTS.md into non-claude panes (codex · gemini/agy · opencode · kimi · cursor — all auto-discover AGENTS.md)
│ ── providers / pipelines / plan ──
├── provider_config.py per-role CLI mapping (claude/codex/gemini/opencode/kimi/cursor) — ~/.takkub/role-providers.json
├── provider_models.py per-provider model override — ~/.takkub/provider-models.json
├── role_models.py    per-role model override, bound to its provider — ~/.takkub/role-models.json
├── provider_install.py shared provider-CLI installer (takkub provider install · doctor --install-providers)
├── provider_state.py  per-provider enable/disable state — ~/.takkub/disabled-providers.json
├── pipeline_config.py pipeline template store (feature/design/quickfix) — ~/.takkub/pipelines.json
├── plan_tier.py       account plan tier (Pro vs Max) — gates [1m] model variant — ~/.takkub/plan.json
├── codex_helper.py    OpenAI Codex CLI one-shot wrapper (non-interactive)
├── gemini_helper.py   Google Antigravity CLI (`agy`) one-shot wrapper — backs `gemini` role (mirror of codex_helper)
│ ── auth ──
├── claude_auth_config.py  optional Claude Code auth override (default = CC's own login)
├── claude_auth_dialog.py  Qt dialog for the auth override
│ ── observability / persistence ──
├── logs_panel.py      bottom dock tailing runtime/events.log
├── token_meter.py     per-pane context occupancy from claude session JSONL usage
├── issues.py          cockpit issue tracker — GitHub Issues backend via `gh` CLI
├── vault_mirror.py    Obsidian write-side: mirror `takkub done` notes/briefs into the vault
├── design_review_html.py  render critic's review .md → self-contained .html
├── chatlog_scanner.py read-only scan of CC per-project session JSONL (resume-brief source)
├── lead_bash_audit.py detect write-y Lead shell commands → JSONL audit record
│ ── diagnostics / verify ──
├── doctor.py          `takkub doctor` — pure-logic env diagnosis (no TCP, no network)
├── skill_audit.py     TF-IDF role-boundary audit — detect overlapping role responsibilities
├── verify.py          `takkub verify` — auto-detect stack + run lint/test gate
├── docs_verify.py     markdown reference verifier — catch stale file/symbol refs
│ ── release / self-update ──
├── release.py         `takkub release` — bump version + roll CHANGELOG + commit/tag
├── claude_update.py   Claude Code CLI self-update: compatibility analysis + controlled rollout
├── rtk_helper.py      one-click install of rtk's PreToolUse Bash hook
├── update_helper.py   git-wrapper behind the status-bar update button
└── update_worker.py   QRunnable that fetches origin/main + reports local_status()
```

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
   - `--setting-sources project,local` (default; skips `~/.claude/settings.json` to avoid the `claude-obsidian` SessionStart crash)
   - `--model`, `--effort`, `--fallback-model` (picked per role tier or global env override)
   - `--plugin-dir` (explicitly passing superpower/skill plugins)
   - `--mcp-config` + `--strict-mcp-config` (force cockpit-managed tool allowlist)
   - `--disallowed-tools Task` (always) + `AskUserQuestion` (teammates only)
   - `--resume <uuid>` if a recent matching session exists in the same cwd
   - `--session-id <uuid>` for fresh spawns to isolate history from other roles/projects
9. `Orchestrator._auto_trust(role_name)` schedules a 500ms-poll loop watching for the "trust this folder" modal and presses Enter when seen.
10. `Orchestrator._send_when_ready(role_name, task)` schedules another 500ms-poll loop watching for the idle `❯` prompt; when seen, writes the task + `\r`.
11. The pane's state goes `empty → active → working`.

## QA Shard Fan-out

When Lead assigns a task with `--shards N` (clamped to 1–8), the orchestrator spawns `N` parallel panes named `<role>#1` to `<role>#N`.
- **Isolation:** Each shard gets its own Chrome port-file and profile-dir (`.takkub/chrome/qa-${SHARD}.port`).
- **Coordination:** Shards share a `ShardGroup` in the orchestrator. When the last shard calls `takkub done`, a consolidated handoff message is sent to Lead.
- **Environment:** `TAKKUB_SHARD` and `TAKKUB_SHARD_TOTAL` are injected into each pane. Without a plan (below), shards self-split work by `TAKKUB_SHARD % TAKKUB_SHARD_TOTAL` (modulo).

### Plan-first fan-out (`--plan`)

`--plan --shards N` (requires `N ≥ 2`; mutually exclusive with `--auto-chain`) turns the fan-out into a **two-phase flow driven entirely by the orchestrator** — the Lead never parses the plan:
1. **Plan phase:** `assign(plan=True)` spawns a single *planner* pane (the bare base role, e.g. `qa`, with `shard_total=0` so it is not itself a shard). `assign()` wraps the task with planner instructions (`_wrap_planner_task`) and records `PaneState.plan_fanout = {shards, cwd, task, plan_file}`. The planner analyses the app and writes a bucket plan JSON (`{"shards": [{"n", "scope", "focus"}, …]}`) to `runtime/qa-plans/<project>-<role>-plan.json` (`_qa_plan_file`), then `takkub done`.
2. **Fan-out phase:** in `done()`, a non-zero `plan_fanout` triggers `_fire_qa_plan_fanout` (PipelineMixin): it reads the plan file and `assign()`s `<role>#1…#k` (staggered by `_SPAWN_STAGGER_MS`), injecting each bucket's `scope`/`focus` into that shard's task. From here it is an ordinary `ShardGroup` fan-out (consolidated handoff on completion). The planner's own per-pane done notice is suppressed in favour of the `[qa plan ready]` message.
- **Degrade:** if the plan file is missing or unparseable, `_fire_qa_plan_fanout` falls back to a plain `k`-shard self-split (modulo) and warns Lead — the parallel run still proceeds instead of stalling. Bucket count is clamped to the requested `N`.

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
- **Reader QThread (per session)** is the only background thread. It does nothing but `proc.read()` + emit.
- **Worker Thread Pool:** Heavy I/O like `git status`, `harvest` (stat-walk), and `doctor` fixes are moved off the main thread to avoid UI freezes.

## IPC schema (CLI ↔ orchestrator, newline-delimited JSON over localhost TCP)

```jsonc
// requests
{"cmd": "list"}
{"cmd": "spawn",     "role": "frontend", "cwd": "C:/path"}
{"cmd": "assign",    "role": "frontend", "cwd": "C:/path", "task": "...", 
                     "requires_commit": false, "auto_chain": false, "shard_total": 0}
{"cmd": "send",      "to":   "backend",  "msg": "...",     "from": "frontend"}
{"cmd": "close",     "role": "frontend"}
{"cmd": "close-all"}
{"cmd": "done",      "from": "frontend", "note": "..."}
{"cmd": "status",    "since": "HH:MM"}
{"cmd": "harvest",   "role": "qa", "since": "HH:MM", "limit": 100}
{"cmd": "harvest-done", "role": "qa", "note": "..."}
{"cmd": "end-session",  "note": "..."}

// responses
{"ok": true,  "msg": "..."}
{"ok": true,  "msg": "status", "status": {"lead": "active", "frontend": "working"}}
{"ok": true,  "msg": "status report", "report": {...}}
{"ok": false, "msg": "<error>"}
```

## Persistence surface

| What | Where | Format |
|---|---|---|
| **Project local** | | |
| Active project / paths / presets | `projects.json` (repo root) | JSON |
| Audit log | `runtime/events.log` | JSONL (append-only) |
| TCP port number | `runtime/port` | plain int |
| Role staging CLAUDE.md | `runtime/agents/<role>/CLAUDE.md` | markdown |
| Pane buffer exports | `runtime/exports/<role>-<ts>.txt` | plain text |
| **User global** | | |
| Role provider map | `~/.takkub/role-providers.json` | JSON |
| Disabled providers | `~/.takkub/disabled-providers.json` | JSON |
| Pipeline templates | `~/.takkub/pipelines.json` | JSON |
| Account plan tier | `~/.takkub/plan.json` | JSON |
| **System** | | |
| Window geometry + splitter | Windows registry `HKCU\Software\agent-takkub\cockpit` | binary |
| Per-role font size | Same registry, key `pane/<role>/font_pt` | int |

## Why some choices look weird

### Why pywinpty WinPTY (not ConPTY) backend?
ConPTY surfaces a visible conhost console window when spawned from a GUI process. WinPTY uses a hidden agent process.

### Why `--append-system-prompt-file`, not inline text?
Role markdown contains backticks, asterisks, and Thai diacritics. Inline-string `--append-system-prompt "<long text>"` runs into Windows argv quoting limits.

### Why `--setting-sources project,local`?
Skips `~/.claude/settings.json` to prevent the `claude-obsidian` SessionStart hook from crashing the cockpit session.

### Why does `Orchestrator.spawn` emit `paneRequested` instead of creating panes itself?
Pane widgets are Qt UI objects — only `MainWindow` knows about the splitter layout.

### Why a TCP server inside a GUI process?
`QTcpServer` lives on the Qt main thread and emits `newConnection` as a normal Qt signal. Every IPC call is serialised through the same event loop as UI events — no lock juggling.

## Test surface

Test surface covers **90 files** (authoritative source: `tests/test_routing_planner.py`).

CI (`.github/workflows/ci.yml`) on `windows-latest`:
1. `pip install -e .[dev]`
2. `ruff check .` + `ruff format --check .`
3. `pytest -v` (Unit + Integration)
