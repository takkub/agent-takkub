# Cross-Platform Robustness Audit — PTY / Process / Terminal / Spawn layer

**Date:** 2026-07-10
**Reviewer:** reviewer (code reviewer)
**Scope:** อ่าน+วิเคราะห์เท่านั้น (ห้ามแก้). เจาะ layer PTY/process/terminal + spawn:
`pty_session.py`, `_pty_backend.py`, `_win_console.py`, `spawn_engine.py` (ทุก provider branch),
`pane_env.py` (`_build_pane_env`/`_build_lead_env` + `_apply_*`), `find_*_executable`
(`config.py`, `codex_helper.py`, `gemini_helper.py`), `terminal_widget.py`.
**Severity:** `breaks-windows` / `breaks-mac` / `breaks-both` / `risky`

> Overall: layer นี้ cross-platform-aware สูงมาก — ทุก `sys.platform=="win32"` มี branch POSIX คู่กัน,
> `_pty_backend` แยก winpty/ptyprocess สะอาด, `_tree_kill` มีทั้ง `taskkill /T` และ `killpg`,
> submit ใช้ `b"\r"` เหมือนกันทั้ง 2 OS, path normalize เป็น forward-slash. findings ด้านล่างคือ
> **asymmetry / gap** ที่หลงเหลือ ไม่ใช่ break ระดับ pandemic. (คนละชุดกับ gemini sweep ที่จับ
> doctor.py/plugin_installer.py — ดู `2026-07-10-xplatform-sweep-gemini.md`)

---

## Findings (เรียงตามความรุนแรง)

| file:line | severity | อธิบาย | fix ที่เสนอ |
| :--- | :--- | :--- | :--- |
| `spawn_engine.py:1056-1177` (codex/gemini return @1162/1091, shell @1011) vs `1427-1429` | **breaks-mac** | **codex / gemini / shell panes ไม่ได้รับ `_apply_color_term` / `_apply_non_interactive_env` / `_apply_mcp_timeout`.** สาม helper นี้ถูกเรียกเฉพาะใน **claude branch** ที่ `spawn_engine.py:1427-1429` ซึ่งอยู่ **หลัง** early-return ของ codex (`return` @1162), gemini (`@1091`), shell (`@1011`). ผลที่พิสูจน์ได้: **(a) breaks-mac** — `_apply_color_term` คือ fix ที่ pane_env.py:272-296 บันทึกไว้ว่ากัน "monochrome TUI บน macOS GUI-launch ที่ไม่มี TERM/COLORTERM inherited". claude ได้ fix นี้ แต่ codex (Rust/ratatui TUI) + agy บน macOS **ไม่ได้** → TUI ขาวดำ (Windows รอด เพราะ CLI force color ผ่าน Win32 console). **(b) both-OS gap** — `_apply_non_interactive_env` (npm_config_yes + GIT_TERMINAL_PROMPT=0, issue #52) ไม่ถูก set ใน codex/gemini/shell pane → agent รัน `npx`/`git` ค้างที่ y/N prompt ได้ ทั้ง 2 OS. ขัด multi-provider directive ("engine env feature ต้องทำงานกับ pane ที่ไม่ใช่ claude"). | ย้าย `_apply_mcp_timeout` / `_apply_non_interactive_env` / `_apply_color_term` ให้เป็น **shared step ที่ทุก branch เรียก** — เช่น เรียกใน `_build_pane_env()` เอง หรือใน `_launch_session()` ก่อน spawn (จุดที่ทุก provider ผ่านแน่นอน). ระวัง: `_apply_mcp_timeout` เป็น claude env — codex/gemini ใช้ MCP config คนละแบบ ค่านี้อาจ no-op กับมัน (ไม่เสียหาย); แต่ color_term + non_interactive จำเป็นทุก provider |
| `spawn_engine.py:976-990` (shell) เทียบ `1088`/`1156`/`1447` + `config.py:604` | **risky** (breaks-windows แบบมีเงื่อนไข) | **ความไม่สอดคล้องเรื่อง winpty ConPTY + full path ที่มี space.** shell branch จงใจ resolve binary แล้วส่ง **basename** (`pwsh.exe`) ให้ winpty พร้อม comment ว่า *"winpty's ConPTY backend can't handle full paths that contain spaces … gets split at the space before quoting takes effect → command not found: C:\\Program"*. แต่ claude/codex/gemini branch ส่ง **full path** เข้า `_launch_session → spawn_pty → subprocess.list2cmdline` (pty_session/_pty_backend.py:59). ถ้าคำเตือนของ shell branch จริง binary ที่ install ใต้ path มี space จะ spawn ไม่ขึ้นบน Windows — และ **claude fallback ที่ `config.py:604 = `Path("C:/Program Files/nodejs")` มี space** ตรงๆ. (production ปัจจุบันรอดเพราะ user's claude อยู่ใต้ `%APPDATA%\npm` ไม่มี space จึงยังไม่ปะทุ) — latent bug สำหรับ Windows user ที่ node/claude/codex/agy อยู่ใต้ `Program Files` | เลือกทางเดียว: **(A)** ถ้า ConPTY จัดการ quoted-space-path ไม่ได้จริง → agent branch ต้อง resolve basename + วาง dir บน PATH เหมือน shell branch (uniform); **(B)** ถ้า `list2cmdline` quoting เพียงพอ (น่าจะใช่) → shell branch's basename workaround เกินจำเป็น ลบทิ้งได้. ต้อง repro บน Windows ด้วย binary path มี space ก่อนตัดสิน — อย่าเดา |
| `gemini_helper.py:48-65` `_default_agy_paths()` | **risky** (breaks-mac) | **agy มี fixed-path fallback เฉพาะ Windows — macOS ไม่มี.** `_default_agy_paths()` คืนเฉพาะ `%LOCALAPPDATA%\agy\bin\agy.exe`. `find_agy_executable` (68-87) = `shutil.which("agy")` → ถ้า None → probe เฉพาะ Windows paths. บน macOS ถ้า Antigravity installer ไม่ลง PATH (คือ failure mode ที่ docstring บันทึกไว้เองว่าเกิดบน Windows @50-57), `find_agy_executable` คืน None → gemini role **degrade เป็น Claude substitute เงียบๆ ทั้งที่ agy ติดตั้งอยู่** = เสีย model diversity โดยไม่รู้ตัว. Windows ได้ fallback, macOS ตกหล่น (asymmetric) | เพิ่ม macOS candidate ใน `_default_agy_paths()` — เช่น `~/Applications/Antigravity.app/...`, `/Applications/Antigravity.app/Contents/MacOS/agy`, `~/.antigravity/bin/agy`, `/opt/homebrew/bin/agy` (gate ด้วย `sys.platform` เหมือน chrome_candidates ที่ spawn_engine.py:1402-1421) |
| `pane_env.py:42-110` `_PANE_ENV_ALLOWLIST` | **risky** (breaks-mac, low) | **allowlist forward `TEMP`/`TMP` (Windows) แต่ไม่มี `TMPDIR` (POSIX/macOS).** pane env ถูก filter ด้วย allowlist (pane_env.py:141) — คีย์นอก list ถูกตัด. บน macOS ค่า temp มาตรฐานคือ `TMPDIR` (`/var/folders/<hash>/…` ต่อ user) ไม่ใช่ `TEMP`/`TMP`. spawned pane บน mac จึง**เสีย `TMPDIR`** → node/npm/`os.tmpdir()`/tool ต่างๆ fallback ไป `/tmp`. ส่วนใหญ่ยังทำงาน แต่ tool ที่พึ่ง per-user TMPDIR (sandbox scope, บาง cache) พฤติกรรมเพี้ยน. คู่กัน `TEMP`/`TMP` มีให้ Windows ครบ แต่ POSIX side ตกหล่น | เพิ่ม `"TMPDIR"` (และพิจารณา `"XDG_RUNTIME_DIR"`, `"XDG_CONFIG_HOME"`, `"XDG_CACHE_HOME"` สำหรับ Linux) เข้า `_PANE_ENV_ALLOWLIST` — parity กับ `TEMP`/`TMP` |
| `config.py:557-592` (find_claude, `.exe` priority) เทียบ `codex_helper.py:31-39` | **risky** (breaks-windows, cosmetic/mitigated) | **find_claude หลบ `.cmd` console-flash แต่ find_codex ไม่หลบ.** `find_claude_executable` ไปไกลเพื่อ resolve **`.exe` จริง** แทน `claude.cmd` เพราะ (config.py:557-559) *".cmd wrapper spawns a visible cmd.exe console window ผ่าน ConPTY"*. แต่ `find_codex_executable` = `shutil.which("codex")` เฉยๆ → บน Windows คืน `codex.cmd` (npm shim) ตรงๆ → cmd.exe console flash ตอน spawn codex pane. **mitigated** โดย `_win_console.snapshot/hide_hwnds` sweep (pty_session.py:677-685) ที่ซ่อน ConsoleWindowClass ใหม่ — จึงเป็น cosmetic race (อาจเห็นวาบก่อนถูกซ่อน). ไม่ break หนัก แต่ inconsistent กับ claude | ถ้า codex ยังมี `.cmd` shim: ทำ resolver แบบ find_claude (หา native `.exe` ก่อน) หรืออย่างน้อย document ว่า hwnd-sweep คือ safety net. ถ้า codex เป็น native binary ล้วนแล้ว (ไม่มี .cmd) → ปิด finding นี้ได้ (ต้องเช็ค npm layout ของ `@openai/codex` version ที่ใช้จริง) |

---

## จุดที่ตรวจแล้ว = PASS (ยืนยันจากโค้ดจริง ไม่ใช่เดา)

- **`_pty_backend.py`** — winpty (Windows) / ptyprocess (POSIX) แยกสะอาด; `read`→bytes normalize ทั้งคู่; `write` รับ str/bytes; cwd-existence guard @128 fail-fast เหมือนกันทั้ง 2 backend (กัน ConPTY hang forever จาก missing cwd). ✓
- **`_tree_kill` (pty_session.py:86-121)** — Windows `taskkill /PID /T /F` + `_CREATE_NO_WINDOW`; POSIX `os.killpg(os.getpgid(pid), SIGKILL)` (ptyprocess spawn via setsid = session leader). คู่กันครบ. ✓
- **submit / paste** — CR submit = `b"\r"` เหมือนกันทั้ง 2 OS (`lead_inbox.py:121/176`); bracketed-paste `\x1b[200~…\x1b[201~` เป็น terminal-level ไม่ผูก OS (`orchestrator_text.py:114-115`); control-strip กัน paste-escape breakout. ✓
- **reader/writer thread teardown** — POSIX blocked `read()` ถูกปลดล็อกโดย killpg+`terminate(force=True)` ที่ทำให้ pty master EOF; `wait(2000)` bounded. ✓ (ไม่มี busy-loop บน POSIX เพราะ read blocking; Windows มี `time.sleep(0.04)` กัน spin บน spurious EOFError)
- **`find_claude_executable` (config.py:554-629)** — Windows `.exe`→`.cmd`→nvm4w/npm/Program Files probe; POSIX `~/.local/bin`, `~/.claude/local`, homebrew, `/usr/local/bin` probe; fallback @591 คืน `which("claude")` ทำให้ mac ใช้ได้. ✓
- **chrome auto-detect (spawn_engine.py:1401-1425)** — win32/darwin/linux candidate ครบ 3 branch, ใช้ `pathlib.Path` + `~/AppData` vs `/Applications`. ✓
- **autonomy_flags (provider_spec.py)** — codex มี `win32` + `default`, claude/gemini มี `default`; `.get(sys.platform, .get("default"))` @1089/1158 ปลอดภัยทุก OS. ✓
- **path normalize (terminal_widget.py:51-58)** — drop/paste path `\\`→`/` ใช้ได้ทั้ง 2 OS. ✓
- **`_win_console.py`** — ทุก entry guard `sys.platform != "win32": return`; `SUBPROCESS_NO_WINDOW=0` บน non-Windows (call-site เดียวกันข้าม OS ได้). ✓

---

## Verdict

ไม่พบ **hard break** ที่ทำให้ layer นี้ใช้ไม่ได้ทันทีบน OS ใดๆ. Finding #1 (color_term/non_interactive ไม่ถึง codex/gemini/mac)
คือตัวเดียวที่ **breaks-mac จริง** ในเชิงประสบการณ์ใช้งาน (monochrome TUI + hang บน npx/git prompt) — ควรแก้ก่อน.
ที่เหลือเป็น latent/asymmetric risk. เนื่องจากเป็น audit อ่านอย่างเดียว (ไม่มี test regression) — รายงานเป็น `takkub done` ปกติ.
