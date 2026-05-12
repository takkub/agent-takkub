# Changelog

All notable changes to agent-takkub. Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [SemVer](https://semver.org/).

## [0.3.8] — 2026-05-12

### Added (token usage meter)
- **Per-pane token badge** ("สรุปการใช้งาน token"): each pane header now shows
  `<prompt> / <limit> · <pct>%` derived from the active claude session's JSONL
  on disk (`~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`). Polls every 5s
  by reading the last assistant turn's `usage` block. Hover for full
  breakdown (input + cache create + cache read + output, model name, limit).
  Colour ramps: grey < 50% → yellow 50-80% → orange 80-95% → red ≥ 95%.
- **Aggregate status-bar meter**: shows `Σ <total> · max <pct>%` summing
  prompt tokens across every active pane. Tooltip lists per-role usage so
  the user can spot which pane is bumping the cap. Headline percentage is
  the **largest single pane's ratio**, not a sum — each pane has its own
  context window so the team-wide ratio is "closest pane to its cap".
- New `token_meter.py` module: `encode_path_for_claude`, `find_latest_session`,
  `read_last_usage`, `format_tokens`, `usage_color`, `context_limit_for_model`.
  Default limit 200k; override via `TAKKUB_CONTEXT_LIMIT` env var for the
  Opus 4.7 [1m] mode.

### Fixed
- **UI freeze during typing** ("อาการ ค้างของการพิมพ์"): Typing while Claude was busy printing large amounts of text caused the entire cockpit UI to freeze. This happened because `winpty.write()` is a blocking call. Fixed by moving `PtySession.write()` to a background `_WriterThread` with a non-blocking queue. Input keystrokes are now immediately queued and the UI remains responsive.
- **Typing delay and ghost characters** ("พิมแล้วดีเลย์/ตัวหนังสือโดนแทนที่"): Switched the PTY backend from WinPTY to ConPTY. WinPTY operates by scraping the hidden console screen buffer on an interval and generating ANSI diffs, which introduced a ~50-150ms roundtrip delay and caused characters to appear out of order or replace each other during rapid typing. ConPTY provides a direct, native ANSI rendering pipeline (same as VS Code and Windows Terminal), resulting in a "super real-time" typing experience.

## [0.3.7] — 2026-05-12

### Added (Lead hybrid policy)
- **Lead direct-edit hybrid policy.** Old guidance was a single soft bullet
  ("Lead ห้ามทำงานเอง") which Lead ignored under pressure — user saw Lead
  doing direct multi-file refactors in pms-web (i18n locales + workload
  page tsx + CSS) instead of delegating to `frontend`. New policy keeps
  flexibility for *meta* work (cockpit config, planning, task specs) but
  draws a hard line for *project* work.
- New decision matrix in `CLAUDE.md` (cockpit):
  - ✅ Lead may edit: cockpit files, plan-time Read/Grep/Glob, single-line
    typos at user-pinned paths, task-spec markdown.
  - 🚫 Lead must delegate: anything under a project path, >1 file,
    >30-line edits in a round, specialist-context work (CSS, API
    contracts, schemas, infra), explicit user assignment.
- **Auto-injected `BLOCKED_DIRS` at every Lead spawn**
  (`orchestrator._render_lead_context`): renders cockpit `CLAUDE.md`
  plus a dynamic section listing the active project's `paths` so Lead
  starts each session knowing the *exact* off-limits directories. Tracks
  `projects.json` so switching projects updates the policy automatically.
- Tools are *not* hard-locked (`--disallowed-tools` unused) — Lead keeps
  Edit/Write for cockpit-side work. The hybrid relies on a sharp,
  spawn-time injected rule rather than coarse tool removal.

### Fixed (stalled-frame bug)
- **Idle pane no longer holds a stale frame.** Symptom the user saw: a
  teammate finishes its turn, the *final* batch of PTY output reaches
  xterm.js, but the DOM paint never happens — the pane sits stuck on
  the second-to-last frame until you press a key or click into it.
  `term.write` had already run; the render simply wasn't painted.
- Root cause: Chromium aggressively pauses requestAnimationFrame and
  paint scheduling for any view that isn't the foreground tab. A
  multi-pane cockpit always has N−1 panes in that state.
- Fix is three-pronged so a single layer failing won't bring the bug
  back:
  1. **Chromium flags** (`app.py`, set before QtWebEngine boots):
     `--disable-background-timer-throttling`,
     `--disable-renderer-backgrounding`,
     `--disable-backgrounding-occluded-windows`,
     `--disable-features=CalculateNativeWinOcclusion`.
  2. **In-page RAF self-loop** (`terminal.html`): a one-line
     `requestAnimationFrame(pulse)` recursive scheduler keeps xterm.js's
     render service warm at the page's native refresh rate.
  3. **Python heartbeat** (`terminal_widget.py`): a 250 ms `QTimer`
     fires `runJavaScript("void 0;")` to force a JS task-queue tick if
     the RAF loop is ever paused for any reason. Cheap on capable
     hardware, harmless on weak.
- User intent for this fix: *"เครื่องฉันแรง อยากให้มันตื่นตัวอยู่ตลอดเวลา"*
  — render service is always on.

[0.3.7]: https://github.com/takkub/agent-takkub/releases/tag/v0.3.7

## [0.3.6] — 2026-05-12

### Removed (final word on local echo)
- **All local echo logic** — for real this time. v0.3.0..v0.3.5 kept
  flip-flopping between "echo locally for snappiness" and "pass-through
  for correctness". Under fast input, claude's TUI renders arrive out
  of order (e.g. a delayed render of `"กพ"` replays *after* the user
  backspaces it away), so a smart-echo gate is not enough — the
  symptom we keep hitting is "I deleted everything, but `กพ` is stuck
  on screen until I press another key".
- xterm.js is now a pure pass-through, same as iTerm / Windows
  Terminal / wezterm. claude is the only writer to the screen. When
  claude is busy, the user perceives a roundtrip of latency per
  keystroke — that is the *correct* terminal behaviour for an
  unresponsive program. The display will never be stuck or desynced.

### Kept
- `window.termSetIdle()` remains as a no-op so the Python-side wiring
  (`AgentPane._sync_idle_flag`, `TerminalWidget.set_idle`) doesn't
  have to be ripped out in lock-step. Reintroducing optimistic
  rendering later just needs to replace the function body.

[0.3.6]: https://github.com/takkub/agent-takkub/releases/tag/v0.3.6

## [0.3.5] — 2026-05-12

### Hardened
- **Idle-flag poll throttled to 150 ms** so the smart-local-echo gate
  doesn't fire 50+ times per second on chatty TUI output. Pyte's
  `is_at_ready_prompt()` scans every line of the screen on each call;
  combined with `outputUpdated` firing per byte chunk, the original
  v0.3.4 wiring was wasting real CPU.
- **Initial idle state forced to `False`** on every pane attach.
  Previously we left `_last_idle = None` and waited for the first state
  flip — meaning a race-condition early keystroke could see the JS
  default (which is whatever the previous pane left there) and local-
  echo into a not-yet-ready terminal.
- **`set_idle()` swallows JS bridge exceptions** so a single
  `runJavaScript` hiccup can't tear the whole `outputUpdated` signal
  chain down.
- **`_sync_idle_flag()` swallows pyte exceptions too** — pyte
  occasionally throws on malformed escape sequences, and we never
  want that to disable the idle gate.

[0.3.5]: https://github.com/takkub/agent-takkub/releases/tag/v0.3.5

## [0.3.4] — 2026-05-12

### Added
- **Smart local echo** — re-introduces optimistic local rendering, but
  only when claude is sitting at the `❯` ready prompt (`is_at_ready_prompt`
  returns true). At that point ink.js re-renders synchronously on every
  keystroke, so local echo + claude's redraw match cell-for-cell and the
  user gets instant feedback again.
- When claude is busy ("Sautéed for 17s") the path collapses to pure
  pass-through, so the v0.3.2-era ghost-character desync can't happen.

### Wiring
- `TerminalWidget.set_idle(bool)` exposes the flag to the JS side via a
  new `window.termSetIdle()` JS function.
- `AgentPane._sync_idle_flag()` listens to `PtySession.outputUpdated`,
  reads `is_at_ready_prompt()` from the pyte screen, and pushes the
  flag whenever it flips. Only edge-triggered updates cross the bridge
  to keep IPC chatter low.

[0.3.4]: https://github.com/takkub/agent-takkub/releases/tag/v0.3.4

## [0.3.3] — 2026-05-12

### Removed
- **All local echo / local backspace handling.** v0.3.0–0.3.2 tried to
  mask the round-trip latency of "type → JS → Python → PTY → claude →
  PTY → JS → render" by writing keystrokes to xterm.js immediately,
  but ink.js TUI input boxes batch their re-renders while claude is
  busy and our stale local state ended up fighting claude's delayed
  redraws. Symptom: typing a char then backspacing repeatedly left a
  ghost char on screen until the user pressed an unrelated key, which
  triggered claude to finally redraw and "consume" the buffered
  backspaces in one go.
- Now xterm.js is a pure pass-through: every keystroke goes straight
  to the PTY and claude is the only source of truth for the input
  area's display. Worst-case latency per keystroke matches every other
  terminal emulator (~roundtrip when claude is busy), but the display
  never desyncs.

[0.3.3]: https://github.com/takkub/agent-takkub/releases/tag/v0.3.3

## [0.3.2] — 2026-05-12

### Fixed
- **Backspace ค้าง** — v0.3.0 local echo wrote each typed char to xterm.js
  instantly but never erased on backspace, so typing "[backend" then
  hitting backspace 8 times left the chars visibly stuck until claude
  caught up and redrew the input area. Local echo now writes `\b \b`
  (erase last cell) when the user presses Backspace/DEL, keeping the
  display in sync with the user's intent even when claude is mid-think.
- **Local-echo filter tightened** — previously `\r`, `\n`, `\t` were
  treated as printable and got written locally, which could nudge the
  cursor in ways that conflicted with claude's redraw. Now only
  0x20..0x7e + non-control multi-byte (Thai, CJK) get local echo;
  everything else passes through to claude untouched.

[0.3.2]: https://github.com/takkub/agent-takkub/releases/tag/v0.3.2

## [0.3.1] — 2026-05-12

### Added
- **`agent-takkub.bat` at repo root** — single-file launcher that newcomers
  can double-click. Checks Python 3.11+ on PATH, checks `claude` CLI on
  PATH, creates `.venv` + installs deps on first run, copies
  `projects.json.example` to `projects.json` and opens it in Notepad if
  missing, then launches the cockpit detached.
- **Quick start** section in `README.md` — 3-step setup with the exact
  commands a fresh user needs (install Python + Claude CLI + clone +
  double-click the launcher).
- **Troubleshooting** table in `README.md` covering the seven most likely
  setup snags (missing Python / claude, sub-window dying, missing
  takkub shim, Thai diacritics, hook errors, wrong Lead cwd).

### Changed
- `scripts/run.bat` is now a thin one-line wrapper that delegates to
  the root `agent-takkub.bat`. Kept for backward compat with existing
  shortcuts / muscle memory.

### Fixed
- `agent-takkub.bat` initial drafts had unescaped `)` inside `echo`
  text blocks (e.g. `echo Log in: claude (one-time)`), which closed
  the surrounding `if` block early and caused unconditional `goto :fail`.
  Replaced with `--` separators.

[0.3.1]: https://github.com/takkub/agent-takkub/releases/tag/v0.3.1

## [0.3.0] — 2026-05-12

### Changed (breaking architecture)

The terminal rendering layer is now **xterm.js running inside a
QWebEngineView**, the same emulator VS Code / Hyper / GitHub Codespaces
ship with. The Iter 1–9 QPlainTextEdit + pyte rebuild pipeline was a
"fake terminal" that hit hard walls on Thai/CJK shaping, alt-screen
scrollback, and TUI form alignment — every "สระหาย / กระตุก / ลบไม่หมด"
report v0.2.x couldn't fully solve.

xterm.js handles these natively: browser layout engine for complex
script shaping (Thai combining marks, BiDi, CJK width), built-in 10k
scrollback, proper mouse modes, and first-class selection/copy/paste.

### Added
- `src/agent_takkub/static/` bundle: `terminal.html`, `xterm.js` 5.5.0,
  `xterm.css`, `addon-fit`, `addon-web-links` — shipped in the package
  via `package_data` so the app works offline.
- `TerminalWidget` rewritten as `QWebEngineView` + `QWebChannel` bridge:
  - `bridge.sendInput(str)` → `inputBytes` signal → PTY
  - `bridge.resize(cols, rows)` → `resized` signal → `PtySession.resize()`
  - `bridge.ready()` → flush bytes queued during boot
- `PtySession.bytesIn(bytes)` signal emitting raw PTY chunks for xterm.js
  to consume directly (no pyte → rich rebuild).
- **Local echo** for printable input in xterm.js so each typed character
  appears the moment the key is pressed instead of waiting for claude's
  ink.js TUI to redraw on the *next* keystroke. Control sequences (Esc,
  arrows, Ctrl-keys, DEL) still go untouched to claude.
- Batched output writes: multiple `write_bytes()` calls within the same
  Qt event-loop tick coalesce into a single `runJavaScript` IPC hop
  (0 ms QTimer). Chatty TUI frames now cost one round trip instead of
  dozens.
- `PyQt6-WebEngine>=6.6` dependency (~150 MB Chromium bundle).

### Kept
- `pyte.Screen` still lives in `PtySession` purely for state-detection
  helpers (`is_at_trust_prompt`, `is_at_ready_prompt`, and `display_lines`
  for export). The double-parse cost buys us keeping every v0.2.x
  orchestrator behaviour — auto-trust, ready-detect, audit log, presets,
  session resume — unchanged.

### Migration
- `pip install -e .` (pulls PyQt6-WebEngine ~150 MB Chromium).
- Same `scripts\run.bat`, same `projects.json`, same `takkub` CLI.
- All v0.2.x behaviour preserved: Lead in project root, role-aware cwd,
  superpowers + agent-skills plugins, audit log, tray notifications,
  bash-friendly `takkub` shim.

### Known caveats
- Per-pane font size shortcut (Ctrl+= / Ctrl+-) wired but untested in the
  xterm.js context; xterm's own Ctrl+= / Ctrl+- works regardless.
- Export pane buffer still goes via pyte (`display_lines`) so it captures
  only the visible viewport. Future patch: switch to xterm.js's full
  buffer (`term.buffer.active`).
- The pyte-mode-detection mouse-wheel path from v0.2.2 is unused —
  xterm.js's built-in scroll handles wheel correctly.

[0.3.0]: https://github.com/takkub/agent-takkub/releases/tag/v0.3.0

## [0.2.4] — 2026-05-12

### Fixed
- **Lead was working on agent-takkub itself, not on the user's project.**
  Lead spawned in `REPO_ROOT` (the cockpit source tree), so its Read/Grep/
  Bash tools all landed in cockpit files instead of the active project's
  code. Lead now spawns in the project root (common parent of all
  `paths`, or first listed path), and the cockpit's `CLAUDE.md` is passed
  via `--append-system-prompt-file` so Lead still knows the `takkub`
  cheatsheet without losing project context.
- `config.lead_cwd()` helper resolves the right directory:
  - `projects.json → projects.<name>.lead` explicit key, if set
  - else the common parent of all `paths` (e.g. `pms/` for `pms-web` + `pms-api`)
  - else the first listed path

### Changed
- Render debounce 20 ms → 0 ms (next-tick coalesce). Qt still batches
  many `outputUpdated` emits within a single event-loop tick into one
  redraw, so we don't thrash, but we also never artificially hold a
  frame back. IME echo and TUI form navigation feel live now.

[0.2.4]: https://github.com/takkub/agent-takkub/releases/tag/v0.2.4

## [0.2.3] — 2026-05-12

### Fixed
- **`takkub: command not found` from Lead's bash** — Lead's Bash tool spawns
  `/usr/bin/bash` (MSYS) which does not auto-append `.cmd` to commands, so
  `bin/takkub.cmd` was invisible to it. Added a POSIX shell shim at
  `bin/takkub` (no extension) that delegates to the same `.venv` Python
  module. cmd.exe/PowerShell still use `bin/takkub.cmd`.
- **UI felt stale ("ไม่ขยับ")** — the v0.2.2 `_last_rendered_rich` diff
  cache was skipping legitimate redraws when row tuples looked identical
  to the previous frame, even though pyte had mutated cursor state /
  refreshed a status line / pulsed a blink. Removed the cache entirely;
  every frame now redraws.
- Bumped debounce 33ms → 20ms (~50 fps) so typing echo feels live again
  while staying cheap enough that idle frames don't thrash.

[0.2.3]: https://github.com/takkub/agent-takkub/releases/tag/v0.2.3

## [0.2.2] — 2026-05-12

### Fixed
- **Thai diacritics rendering** — `QTextCharFormat.setFont(QFont(widget.font()))`
  was collapsing the families fallback chain in some Qt builds, so combining
  marks (◌ิ ◌ี ◌่ ◌้ ◌์ ฯลฯ) silently disappeared. Switched to
  `setFontFamilies(...)` + individual `setFontWeight/Italic/Underline` which
  preserves per-glyph fallback through Tahoma/Leelawadee UI.
- **Typing stutter** — added a `_last_rendered_rich` diff cache so identical
  screen states skip the full QTextDocument rebuild (~360 insertText calls).
  pyte fires `outputUpdated` for every byte chunk including no-op sequences
  (mouse-mode toggles, cursor save/restore), and the old path paid the rebuild
  on every keystroke.
- Bumped debounce 16ms→33ms (30fps) so typing storms collapse into fewer
  frames.
- Auto-scroll-to-bottom only fires when the user was already at the bottom
  before the refresh. Scrolling up to inspect history no longer gets yanked
  away by the next pyte update.

### Added
- **Smart mouse-wheel forwarding** — when claude has SGR mouse tracking on
  (mode 1006, the modern default), wheel events go out as proper
  `\x1b[<64;1;1M` / `\x1b[<65;1;1M` press events so claude scrolls its own
  buffer smoothly. Falls back to PgUp/PgDn when mouse tracking is off.
- `AgentPane._refresh_terminal` reads `screen.mode` and sets
  `TerminalWidget.mouse_tracking_on` accordingly on every frame.

[0.2.2]: https://github.com/takkub/agent-takkub/releases/tag/v0.2.2

## [0.2.1] — 2026-05-12

### Fixed
- Default `--setting-sources` reverted to `project,local`. The v0.2.0 switch to
  `user,project,local` re-exposed claude-obsidian 1.4.3's `SessionStart` hook
  bug (`ToolUseContext is required for prompt hooks. This is a bug.`) inside
  every spawned pane.
- Cleared `presets: ["frontend"]` from the shipped `projects.json`. Auto-spawn
  was firing on every cockpit launch regardless of whether the user wanted a
  frontend pane. Lead now stays alone until you `takkub assign` or click "+ pane".

### Added
- `_default_plugin_dirs()` + explicit `--plugin-dir` args so spawned agents
  still inherit **superpowers** and **agent-skills** even though user-level
  settings are skipped. claude-obsidian is intentionally excluded until its
  hook is fixed upstream.
- `TAKKUB_EXTRA_PLUGINS` env var (semicolon-separated paths) to override the
  default plugin allowlist — set to empty string to suppress, or point at
  custom plugin directories.

[0.2.1]: https://github.com/takkub/agent-takkub/releases/tag/v0.2.1

## [0.2.0] — 2026-05-12

### Changed
- `--setting-sources` default flipped from `project,local` to `user,project,local`
  so spawned agents inherit the user's installed Claude Code plugins (superpowers,
  agent-skills, claude-obsidian) and MCP servers. The original Iter 1 SessionStart
  hook bug that motivated the previous isolation appears resolved in claude-obsidian 1.4.3.

### Added
- `TAKKUB_SETTING_SOURCES` env var to override the default (e.g.
  `TAKKUB_SETTING_SOURCES=project,local` to fall back to the isolated v0.1 behaviour
  if a global plugin misbehaves).
- Orphan cleanup hook in `app.py`: atexit + SIGINT/SIGTERM/SIGBREAK handlers terminate
  every spawned claude/winpty-agent before the Qt process exits, so a crash or kill
  can't leave child processes pinned to the venv.
- Lead's `CLAUDE.md` now starts with a takkub quick-reference table + a "Tooling
  available to agents" section pointing at superpowers / agent-skills / MCP. Lead
  sees this on every session start, no more "what commands exist?".

[0.2.0]: https://github.com/takkub/agent-takkub/releases/tag/v0.2.0

## [0.1.0] — 2026-05-12

First release. Replaces the tmux-based `agent-teams` setup with a native Windows desktop cockpit. Built in 9 iterations on the same day.

### Added — Iter 1 (baseline)
- PyQt6 main window with 3-column splitter (Lead · middle · right)
- `pywinpty` PTY backend, `pyte` ANSI screen model
- TCP-based `takkub` CLI (list / spawn / assign / send / close / done) for agent-to-orchestrator IPC
- Initial migration of 7 role definitions from `agent-teams` (replaced tmux-send-keys with `takkub` CLI calls)
- `scripts/run.bat` launcher that creates the .venv on first run

### Fixed — Iter 1.5 (post-launch debugging)
- Hidden `cmd.exe`/`conhost.exe` console window after spawn (`ConsoleWindowClass` SW_HIDE diff)
- Use `pythonw.exe` + `start ""` in `run.bat` so the launcher batch exits immediately
- pywinpty `read(size=...)` signature fix (`num_bytes` kwarg was wrong)
- pywinpty `write()` expects `str` not `bytes` — silent TypeError was eating every keystroke
- EOFError handling: check `isalive()` before treating an empty read as termination
- Thai diacritic regression after rich rendering — preserve `QFont` family fallback chain inside `QTextCharFormat`

### Added — Iter 2
- Auto-trust folder prompt (poll for "trust this folder" modal → send Enter)
- Auto-detect idle `❯` prompt before pasting `assign` task (replaces 12s fixed wait)
- Mouse wheel forwarded as PgUp/PgDn so claude's alt-screen scroll works
- Pane fully removed from layout on close (was leaving an empty placeholder)

### Added — Iter 3
- ANSI colour rendering via `QTextCharFormat` cache + custom 16-colour palette (bold/italic/underline/reverse honoured)
- Spinner animation + elapsed-time counter on `working` panes
- Project switcher combo in status bar (writes back to `projects.json`)
- "+ pane" button to open a default or custom role

### Added — Iter 4
- Window geometry + splitter sizes persisted via `QSettings`
- Role-aware default cwd resolution (frontend→web, backend→api, ...)
- `--append-system-prompt-file <role.md>` so specialist override applies even when cwd is the project root
- Event audit log at `runtime/events.log` (JSONL: spawn/assign/send/close/done)
- Cleaned redundant 2.7s close path in main_window

### Added — Iter 5
- Crash recovery: `_expected_exit` flag distinguishes user-close from claude crash; crashed panes show orange "exited" state with respawn affordance
- Spawn errors surfaced in status bar
- Font-size shortcuts inside terminal (Ctrl+= / Ctrl+- / Ctrl+0)
- Lead pane shows active project name in header (`Lead · pms`)
- Verified `takkub done` end-to-end (done → 2.5s grace → orchestrator.close → pane removed)

### Added — Iter 6
- Bottom dock `LogsPanel` that tails `runtime/events.log` every 1s
- F1 / `?` help dialog with `takkub` cheatsheet + shortcuts
- "⟶ assign" quick-assign button (role picker + multi-line task input)
- `takkub close-all` command (closes every teammate, keeps Lead)

### Added — Iter 7
- Session resume: `claude --continue` passed automatically on respawn within 5min in the same cwd
- Desktop notification (`QSystemTrayIcon`) when an agent calls `takkub done`
- Export pane buffer to `.txt` via `⤓` button in the header (`runtime/exports/<role>-<ts>.txt`)
- Per-role font size persisted in `QSettings`

### Added — Iter 8
- Pane header shows cwd basename (`Frontend · pms-web`)
- Status bar live count: active panes + working panes (2s tick)
- Auto-spawn presets per project (`projects.json` → `presets: ["frontend", "backend"]`)
- Logs panel: filter by event type + role substring

### Added — Iter 9
- Pane minimise/restore toggle (`▾`/`▸` button collapses the body to the header strip)
- Logs panel text search (case-insensitive substring across rendered line)
- Custom-role colour picker via `QColorDialog` in the "+ pane → custom..." flow
- README rewritten to reflect all current features

### Verification — Iter 9 (final)
- End-to-end multi-agent flow tested live with the real PMS project:
  - backend created `pms-api/src/health/health.controller.ts` + module wiring
  - frontend waited for backend's `takkub send` message before implementing `pms-web/app/agent-takkub-test/page.tsx` with Ant Design (agent inspected project conventions instead of using the suggested shadcn)
  - both agents called `takkub done`; both panes auto-closed without manual intervention
- Multi-agent peer-to-peer comms + auto-close lifecycle verified against `runtime/events.log`

[0.1.0]: https://github.com/takkub/agent-takkub/releases/tag/v0.1.0
