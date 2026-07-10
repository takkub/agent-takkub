# Cross-Platform Robustness Audit — CONSOLIDATED (Mac + Windows)

**Date:** 2026-07-10
**Method:** 3-model fan-out (Claude `reviewer` · `codex` · `gemini`), Lead-consolidated + adversarially verified against real code/disk.
**Question:** ช่องโหว่ที่ทำให้ cockpit ใช้งานไม่ได้บน Mac หรือ Windows.
**Source reports:** `2026-07-10-xplatform-pty-reviewer.md` · `2026-07-10-xplatform-paths-codex.md` · `2026-07-10-xplatform-sweep-gemini.md`

**Confidence tags:** `CONFIRMED` = Lead verified against real code/disk this session · `HIGH` = line-cited by a reviewer, not independently re-run · `PLAUSIBLE` = reported, needs a verify pass before fixing.

---

## 🔴 CRITICAL — fix first

### C1. `decode_project_dir()` is lossy → resume/session-scan silently skips every project whose path contains `-` `_` `.` or space
**Severity: breaks-both · Confidence: CONFIRMED (disk + python proof)** · found by **codex**, verified by Lead

Claude Code encodes a cwd into its `~/.claude/projects/<name>` dir by mapping **every** non-alphanumeric char to `-` (not just path separators). `decode_project_dir()` reverses this by turning **every** `-` back into a path separator — so the round-trip is irreversible for any real project name.

**Proof (this machine, live):**
```
dir on disk : C--Users-monch-WebstormProjects-agent-takkub
decode_project_dir → C:\Users\monch\WebstormProjects\agent\takkub
real cwd            → C:\Users\monch\WebstormProjects\agent-takkub   ← MISMATCH
```
The `-` in `agent-takkub` becomes `\`. Every downstream `.resolve()` equality check fails.

**Blast radius (all use the same broken decode-then-compare):**
- `chatlog_scanner.list_recent_lead_sessions` — **the desktop ↻ Resume picker shipped in `1d7b5bc`** → shows "ไม่พบ session" even when sessions exist. (This is the real reason the `1d7b5bc` runtime smoke returned 0 sessions — misattributed to "profile env" at the time.)
- `remote/notify.list_recent_lead_sessions` — the **mobile PWA** resume picker, same failure.
- `spawn_engine._resume_uuid_matches_cwd` — explicit resume validation. Desktop (`user_actions.py:306`) and remote (`remote/api.py:265`) both `orch.close(lead)` **before** calling `spawn(resume_uuid=...)`, so a hyphenated project → resume rejected → **user left with the Lead pane closed AND resume failed.**

**Fix (codex):** stop decoding for identity. Add one `encode_path_for_claude(cwd)` helper (or lift `token_meter.encode_path_for_claude`) and compare `dir.name == encode_path_for_claude(cwd_resolved)`. Factor a single `session_project_dir_for_cwd(config_dir, cwd)` used by desktop picker + remote picker + `_resume_uuid_matches_cwd` so they list the exact dir instead of scan-and-decode. Keep `decode_project_dir()` for display/search best-effort only. In the resume flow, **prevalidate before `orch.close`**. Add regression tests for `agent-takkub`, `my_app.web`, and a spaced path.

---

## 🟠 HIGH

### H1. Non-claude panes (codex / gemini / shell) skip `_apply_color_term` / `_apply_non_interactive_env` / `_apply_mcp_timeout`
**Severity: breaks-mac (+ both-OS hang) · Confidence: HIGH (reviewer line-cited)** · found by **reviewer**

`spawn_engine.py:1427-1429` calls those three env helpers **only in the claude branch**, which runs *after* the early-returns of codex (`@1162`), gemini (`@1091`), shell (`@1011`).
- **breaks-mac:** `_apply_color_term` is the documented fix (`pane_env.py:272-296`) for monochrome TUI on macOS GUI-launch (no inherited `TERM`/`COLORTERM`). claude gets it; **codex (ratatui) + agy do not → ขาวดำบน mac.** Windows survives (Win32 console forces color).
- **both-OS:** `_apply_non_interactive_env` (`npm_config_yes` + `GIT_TERMINAL_PROMPT=0`, issue #52) never set for codex/gemini/shell → those panes **hang on `npx`/`git` y/N prompts** on both OS.
- Violates the multi-provider directive ("engine env feature ต้องทำงานกับ pane ที่ไม่ใช่ claude").

**Fix:** move the three `_apply_*` to a shared step every branch passes through (inside `_build_pane_env()` or `_launch_session()` pre-spawn). `_apply_mcp_timeout` may be a no-op for codex/gemini (different MCP config) — harmless; `color_term` + `non_interactive` are needed by all providers.

---

## 🟡 MEDIUM

### M1. `doctor.py` auth check: wrong filename + no macOS Keychain probe
**Severity: misleading diagnostic (both OS) · Confidence: CONFIRMED** · found by **gemini**, verified by Lead

`doctor.py:112,140` check `~/.claude/credentials.json`, but the real file is **`.credentials.json`** (leading dot). Result: Windows branch always reports SKIP, POSIX branch always reports **false WARN "credentials.json not found"** even when logged in. On macOS the credential lives in the **login Keychain** (service `Claude Code-credentials`), not a file at all — so even fixing the filename leaves a Mac false-WARN. Not a usage break (cockpit still runs), but `takkub doctor` lies and tells logged-in users to re-login.

**Fix:** correct `credentials.json` → `.credentials.json`; on macOS probe the Keychain (`security find-generic-password -s "Claude Code-credentials" -w`, as `limit_status.py` already does) before warning.

### M2. Hardcoded `~/.claude/plugins/` ignores profile / isolated config
**Severity: breaks-both (isolated/profile mode) · Confidence: PLAUSIBLE** · found by **gemini**

`lead_context.py:640`, `pane_tools_dialog.py:56`, `plugin_installer.py:125` hardcode `~/.claude/plugins/`. Installed/profile mode uses `default_claude_config_dir()` = `DATA_HOME/claude-config` (or a profile's `CLAUDE_CONFIG_DIR`) → GUI plugin checks look at the wrong dir vs. what panes actually run with. **Verify** each site before fixing (some may be intentional dev-mode reads).
**Fix:** resolve via `config.default_claude_config_dir()` / active profile's `CLAUDE_CONFIG_DIR`.

### M3. `plugin_installer` spawns bare `claude` + drops `CLAUDE_CONFIG_DIR`
**Severity: breaks-windows (conditional) + isolation · Confidence: PLAUSIBLE** · found by **gemini**

`plugin_installer.py:102` `subprocess.run(["claude", ...], shell=False)` — on Windows a `claude.cmd` npm shim won't resolve without `shell=True` → `FileNotFoundError`. Also doesn't propagate active `CLAUDE_CONFIG_DIR`, so GUI-installed plugins land in global `~/.claude` not the active profile.
**Fix:** use `config.find_claude_executable()` (absolute path, already dodges `.cmd`) and pass the active env.

---

## 🟢 LOW / LATENT (asymmetric risk, not live breaks)

| id | file | sev | one-liner | source |
|---|---|---|---|---|
| L1 | `spawn_engine.py` shell vs agent branch + `config.py:604` | risky/breaks-win | shell branch sends winpty a **basename** to dodge "ConPTY can't handle spaced full paths"; claude/codex/gemini send full path. `Program Files` fallback has a space → latent Windows spawn-fail. Needs a Windows repro with a spaced binary path before choosing to unify-or-delete the workaround. | reviewer |
| L2 | `gemini_helper.py:48-65` | risky/breaks-mac | `_default_agy_paths()` has **Windows-only** fixed-path fallback; on mac if agy isn't on PATH, gemini silently degrades to Claude-substitute even when agy is installed. Add mac candidates (`/Applications/Antigravity.app/...`, `/opt/homebrew/bin/agy`) gated by `sys.platform`. | reviewer |
| L3 | `pane_env.py:42-110` allowlist | risky/breaks-mac (low) | forwards `TEMP`/`TMP` but not POSIX **`TMPDIR`** (nor XDG_*) → mac panes lose per-user tmp, fall back to `/tmp`. Add `TMPDIR`. | reviewer |
| L4 | `codex_helper.py:31-39` | risky/breaks-win (cosmetic) | `find_codex_executable` = bare `which` → returns `codex.cmd` → cmd.exe console flash on spawn (mitigated by `_win_console` hwnd-sweep). `find_claude` avoids this; codex doesn't. | reviewer |
| L5 | `spawn_engine.py:1642-1650`, `orchestrator.py:2033` | risky | 5-min auto-resume compares cwd as **raw strings** → Windows case/slash/short-long or POSIX symlink variants disable intended `--resume` recovery (starts fresh session). Normalize with `Path.resolve()` + `os.path.normcase()`. | codex |
| L6 | `config.py:363,392,398` | risky | `lead_cwd()` returns raw configured strings; relative project paths + `os.path.commonpath()` can yield `.` → Lead spawns in cockpit cwd, not the project. Absolutize at load. | codex |

---

## ⚙️ META — cockpit robustness (observed live this session)

**Codex pane self-updated mid-task and exited**, silently dropping the in-flight audit task with no output and no auto-retry (`changed 2 packages… 🎉 Update ran successfully! Please restart Codex.`). A provider CLI's own auto-update can kill an assigned task; the orchestrator did not detect the abnormal exit + re-deliver. (Distinct from #26 blind-paste-delivery.) Worth a dedicated issue: detect provider-update-exit and auto-re-assign, or pre-empt the update splash on spawn.

---

## Recommended order

1. **C1** (decode/resume) — breaks a just-shipped feature for the flagship project name; one shared encode-helper fixes desktop + mobile + validation. **Do first.**
2. **H1** (non-claude env) — real mac breakage + both-OS hang; small move-to-shared-step.
3. **M1/M3** — quick correctness (doctor filename + installer exe/env).
4. **M2, L1–L6** — verify-then-fix as capacity allows.
5. **Meta** — file the provider-update-exit robustness issue.

## Verified PASS (no action) — from reviewer's read
`_pty_backend` winpty/ptyprocess split · `_tree_kill` taskkill/killpg pair · CR/paste identical both OS · reader/writer teardown · `find_claude_executable` POSIX branch · chrome auto-detect 3-branch · `_win_console` guards · `worktree_root/dest` resolve+sanitize · `user_profile.config_dir_for`.
