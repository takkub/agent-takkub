# macOS Port Plan — Takkub Cockpit

**Goal:** ทำให้ Takkub Cockpit รันบน macOS ได้ครบทุกฟีเจอร์เทียบเท่า Windows (multi-project tabs, PTY panes, orchestrator IPC, watchdogs, token meter, MCP injection, vault mirror, codex pane, ฯลฯ)

**Status:** draft, ยังไม่เริ่ม implement
**Owner:** Lead
**Estimated effort:** ~14 ชม. (ไม่รวม corner cases)

---

## 1. Scan สรุป — Windows surface area

| ไฟล์ | Windows dependency | Mac fix |
|---|---|---|
| `src/agent_takkub/pty_session.py` | `pywinpty` + ConPTY backend (~330 บรรทัด) | abstraction layer → `ptyprocess` (POSIX) |
| `src/agent_takkub/_win_console.py` | ctypes `user32` EnumWindows / ShowWindow (hide conhost) | no-op บน Mac (POSIX PTY ไม่ surface console window) |
| `src/agent_takkub/config.py:194-235` | `claude.exe` / `claude.cmd` resolution + `C:/nvm4w/...` hardcode | `which("claude")` + nvm / asdf / Homebrew path probing |
| `src/agent_takkub/orchestrator.py:841-843` | Chrome.exe hardcoded paths (Browser MCP) | เพิ่ม `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome` |
| `src/agent_takkub/rtk_helper.py:33-37` | `rtk.exe` + Unix fallback (`~/bin/rtk`, `/usr/local/bin/rtk`, `/opt/homebrew/bin/rtk`) | **ผ่านอยู่แล้ว** ไม่ต้องแก้ |
| `src/agent_takkub/app.py:66` | Windows Job Object (kill children with parent) | macOS = `os.setpgrp()` + SIGTERM ทั้ง process group |
| `pyproject.toml` | `pywinpty>=2.0;sys_platform=='win32'` | เพิ่ม `ptyprocess>=0.7;sys_platform!='win32'` |

---

## 2. Portable เรียบร้อย — **ไม่ต้องแตะ**

- ทั้งหมดของ PyQt6 UI (tabs, splitters, dock, statusbar, tray icon, dialogs)
- Orchestrator core (state machines, idle/stuck watchdogs, role gate, IPC dispatch)
- `cli_server.py` + `cli.py` (loopback TCP socket cross-platform)
- Token meter / chatlog scanner (read JSONL จาก `~/.claude/projects/` — schema เหมือนกัน Mac/Win)
- Vault mirror (`~/WebstormProjects/second-brain` — Path API cross-platform)
- Codex helper (`shutil.which("codex")` ทำงานทุก platform)
- Update helper / RTK helper / shared MCP injection (subprocess + JSON file)
- Events log, decision notes, hot.md snapshot
- xterm.js terminal widget (QWebEngine render layer)

---

## 3. Architecture — PTY abstraction layer

แตก `pty_session.py` เป็น 3 ไฟล์ → **ของเดิม 0 ไฟล์อื่นต้องแก้** (public API คงเดิมทุก signature)

```
pty_session.py          ← public API (PtySession class, signals unchanged)
  ↓ delegates to
_pty_backend.py         ← factory: เลือก backend จาก sys.platform
  ↓
_pty_windows.py         ← pywinpty + ConPTY + console hwnd hide (โค้ดเดิมยกมา)
_pty_posix.py           ← ptyprocess.PtyProcess + SIGWINCH resize
```

**Public API ที่ต้องเทียบเท่าทั้ง 2 backend:**

| Method | Windows (pywinpty) | POSIX (ptyprocess) |
|---|---|---|
| `spawn(argv, cwd, env)` | `winpty.PtyProcess.spawn(cmd, ...)` | `PtyProcess.spawn(argv, ...)` |
| `read(size)` | `proc.read(size)` (EOFError on empty) | `proc.read(size)` (returns b"" on EOF) |
| `write(data: str)` | `proc.write(data)` | `proc.write(data.encode())` |
| `setwinsize(rows, cols)` | `proc.setwinsize(rows, cols)` | `proc.setwinsize(rows, cols)` |
| `terminate(force=True)` | `proc.terminate(force=True)` | `proc.terminate()` + SIGKILL fallback |
| `isalive()` | `proc.isalive()` | `proc.isalive()` |
| `exitstatus` | `proc.exitstatus` | `proc.exitstatus` |

`ptyprocess` API ใกล้เคียง pywinpty 1:1 — mapping ตรงไปตรงมา

---

## 4. งานแยกเป็น Phase (เรียงตามลำดับ execute)

### Phase 1 — PTY layer ให้ cross-platform (8 ชม.)

1. เพิ่ม `ptyprocess>=0.7;sys_platform!='win32'` ใน `pyproject.toml`
2. สร้าง `src/agent_takkub/_pty_backend.py` — factory function `make_backend()` เลือกจาก `sys.platform`
3. ย้าย winpty code เดิม (รวม console hwnd hide logic) → `src/agent_takkub/_pty_windows.py`
4. เขียน `src/agent_takkub/_pty_posix.py` mirror API ครบทุก method
5. ปรับ `pty_session.py` ให้ import จาก backend factory แทน import winpty ตรงๆ
6. **Acceptance:** Windows ใช้งานต่อได้ปกติ ไม่ break อะไร + Mac smoke test spawn lead pane ได้

### Phase 2 — Path / executable resolution (2 ชม.)

7. `find_claude_executable()` ใน `config.py` → branch ตาม platform
   - Mac fallbacks: `/opt/homebrew/bin/claude`, `/usr/local/bin/claude`, `~/.nvm/versions/node/*/bin/claude`, `~/.volta/bin/claude`
8. Chrome paths ใน `orchestrator.py:841-843` → เพิ่ม Mac entry `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`
9. `app.py` child cleanup → Mac branch ใช้ `os.setpgrp()` ตอน spawn + `os.killpg(pgid, SIGTERM)` ตอน parent exit
10. **Acceptance:** `find_claude_executable()` คืนค่า valid path บน Mac + chrome MCP launch ได้

### Phase 3 — Default paths / config template (1 ชม.)

11. สร้าง `projects.example.json` ใช้ `~/WebstormProjects/...` (ไม่มี drive letter) เป็น template
12. ปรับ `CLAUDE.md` เพิ่ม section "macOS setup" สั้นๆ:
    - prerequisites: Python 3.11+, `claude` CLI, optional `codex` / `rtk`
    - install: `pip install -e .` (จะ auto-pick ptyprocess marker)
    - launch: `python -m agent_takkub`
    - troubleshooting: PyQt6-WebEngine arm64 wheel availability
13. `README.md` toggle Windows-only → cross-platform language
14. **Acceptance:** new Mac user clone repo + edit projects.json + launch ได้ภายใน 5 นาที

### Phase 4 — UX / cosmetic polish (1 ชม.)

15. Terminal widget font fallback — เลือก default monospace ตาม platform (`SF Mono`/`Menlo` บน Mac, `Cascadia Code`/`Consolas` บน Win)
16. Window controls (close/min/max) — PyQt6 native style ผ่านอยู่แล้ว แค่ verify
17. System tray icon — verify ใช้ได้บน macOS (อาจต้อง separate icon file สำหรับ menubar)
18. Keyboard shortcuts — `Ctrl+W` close tab บน Mac ควรเป็น `Cmd+W` (Qt auto-map ผ่าน `QKeySequence.StandardKey.Close`)
19. **Acceptance:** UI ดู native บน Mac ไม่มี artifact

### Phase 5 — Verify (2 ชม.)

20. รัน existing `pytest` test suite บน Mac → fix Mac-specific failures (ถ้ามี)
21. Smoke test manually:
    - spawn lead pane → spawn frontend teammate → assign task → done → close
    - paste-bracketed payload ยาวๆ ไม่ลืม head
    - trust prompt auto-accept ทำงาน
    - idle watchdog fire reminder หลัง 45s
    - stuck watchdog respawn หลัง 10 min
    - codex pane launch ได้ + non-interactive `codex exec`
    - MCP tools (playwright, chrome-devtools, pms) ใช้ได้จาก pane
    - vault mirror เขียน `hot.md` + decision notes
22. Token meter อ่าน JSONL บน Mac ถูกต้อง (encoded project dir format อาจต่าง)
23. **Acceptance:** ทุก feature ทำงานบน Mac เทียบเท่า Windows (checklist ใน section 7)

---

## 5. Risk points

1. **Bracketed paste timing** (`_enter_delay_ms`) — PTY latency บน Mac ต่ำกว่า Win อาจต้อง tune ใหม่ ถ้าเกิด race เรื่อง head-of-message ถูกตัด
2. **Trust prompt detection** (`is_at_trust_prompt`) — parse screen text ใช้ key phrases เดิมน่าจะใช้ได้ แต่ต้องดู claude CLI บน Mac ว่า render เหมือนเป๊ะ (ไม่มี ANSI difference)
3. **`projects.json` path migration** — user clone repo จาก Windows ต้องแก้ paths ก่อนใช้งานครั้งแรก (UX ไม่ใช่ bug)
4. **PyQt6-WebEngine arm64 wheel** บน Apple Silicon — ต้องยืนยันว่ามี wheel พร้อม ไม่ตก Rosetta
5. **Child process cleanup** — Windows ใช้ Job Object kill ทั้ง tree ตอน parent exit; Mac ต้อง track pgid เองและส่ง SIGTERM ลูก ๆ ใน orchestrator shutdown
6. **`subprocess.list2cmdline`** — Windows-specific argument quoting ใน `pty_session.py:130` POSIX ใช้ `shlex.join` แทน
7. **Encoded project dir** ใน `chatlog_scanner.py` / token_meter — Claude Code encode path เป็น dir name สูตรเดียวกันทุก OS แต่ separator ต่างกัน (`\` vs `/`) ทำให้ encoded name ไม่ตรง ต้อง decode ใหม่ใน `decode_project_dir`

---

## 6. Out of scope

- Linux support (focus Mac เท่านั้น แต่ POSIX backend ส่วนใหญ่จะรัน Linux ได้ฟรี)
- Native macOS .app bundle (เก็บไว้ post-MVP — รัน `python -m agent_takkub` ก็พอ)
- Apple Silicon performance tuning (ใช้ default ก่อน)
- Cross-platform installer / homebrew formula (post-MVP)

---

## 7. Acceptance checklist (final smoke test บน Mac)

- [ ] `pip install -e .` ติดตั้งสำเร็จไม่มี error
- [ ] `python -m agent_takkub` เปิด cockpit + Lead pane render
- [ ] Multi-project tabs: เปิด 2 projects, สลับ tab ได้, active_project อัปเดต
- [ ] `takkub assign --role frontend --cwd <path> "task"` spawn pane + paste task
- [ ] `takkub send --to backend "msg"` route ถูก + CC Lead
- [ ] `takkub done note` ปิด pane + mirror decision note
- [ ] `takkub close --role X` ปิด pane เฉพาะ role
- [ ] Role gate: teammate pane เรียก `takkub assign` → exit 1
- [ ] Idle watchdog: pane idle 45s + working state → inject reminder
- [ ] Stuck watchdog: PTY silent 10 min → auto close + respawn `--continue`
- [ ] Resume window: respawn ภายใน 5 นาที pick up `--continue` + emit paneResumed
- [ ] Token meter: pane header show prompt token + status bar total
- [ ] Vault mirror: `hot.md` rewrite ทุก 60s + every `done`
- [ ] MCP shared injection: Playwright + Chrome DevTools + pms ใช้ได้จาก pane
- [ ] Codex pane: spawn binary โดยตรง + autonomy flags + trust auto-accept
- [ ] RTK install button: เขียน `.claude/settings.json` ถูกต้อง
- [ ] Self-update chip: `git fetch` poll ทุก 5 นาที + pull ได้
- [ ] Events log dock: tail `runtime/events.log` แสดง spawn/assign/send/done/close

---

## 8. Implementation order recommendation

เริ่ม **Phase 1 PTY abstraction ก่อนสุด** เพราะ:
- เป็น risk หลัก (port งานยากที่สุด)
- เป็น blocker ของทุกอย่างที่เหลือ
- ถ้าทำเสร็จแล้ว ที่เหลือเป็น path/string/cosmetic ซึ่ง low-risk

หลัง Phase 1 ผ่าน smoke test บน Mac (spawn pane → render output ได้) ที่เหลือทำขนานได้

---

## 9. ไฟล์ใหม่ที่จะถูกสร้าง

```
src/agent_takkub/_pty_backend.py     (factory ~30 บรรทัด)
src/agent_takkub/_pty_windows.py     (โค้ดเดิมจาก pty_session.py + _win_console.py รวมกัน)
src/agent_takkub/_pty_posix.py       (ptyprocess wrapper ~150 บรรทัด)
projects.example.json                (template สำหรับ Mac users)
docs/MACOS_PORT_PLAN.md              (ไฟล์นี้)
```

## 10. ไฟล์ที่จะถูกแก้ไข

```
pyproject.toml                       (เพิ่ม ptyprocess dep)
src/agent_takkub/pty_session.py      (refactor → delegate ไป backend)
src/agent_takkub/config.py           (find_claude_executable cross-platform)
src/agent_takkub/orchestrator.py     (Chrome path fallback)
src/agent_takkub/app.py              (child cleanup ผ่าน pgid)
src/agent_takkub/terminal_widget.py  (font fallback)
CLAUDE.md                            (เพิ่ม macOS setup)
README.md                            (cross-platform language)
```

ไฟล์ที่ **ไม่แตะเลย** ตามแผน: `main_window.py`, `agent_pane.py`, `cli.py`, `cli_server.py`, `roles.py`, `project_tab.py`, `token_meter.py`, `chatlog_scanner.py`, `update_helper.py`, `codex_helper.py`, `rtk_helper.py`, `shared_dev_tools.py`, `logs_panel.py`
