# Cross-Platform Audit — Engine/Path/Env Fixes (backend#1)

**Date:** 2026-07-10
**Scope:** `docs/reviews/2026-07-10-xplatform-CONSOLIDATED.md` findings assigned to the engine/path/env group — C1, H1, L5, L6, L2, L3, L4, L1.
**Excluded (other instances):** doctor.py, plugin_installer.py, lead_context.py, pane_tools_dialog.py (M1/M2/M3).

All 8 findings closed FIXED. Every fix has a Win+Mac branch (or is platform-neutral), a regression test, and does not change provider-substitution behavior. Targeted tests only, per project policy — full suite not run.

---

## C1 (CRITICAL, breaks-both) — `decode_project_dir()` lossy round-trip

**Fixed.** Added `token_meter.session_project_dir_for_cwd(config_dir, cwd)` — encodes `cwd` forward via the existing `token_meter.encode_path_for_claude()` and returns the exact `<config_dir>/projects/<encoded>` dir, instead of scanning every project dir and reverse-decoding names for an equality check (`decode_project_dir()` maps *every* non-alnum char to `-`, so it can't tell a literal `-`/`_`/`.`/space in the original path apart from an encoded separator).

Changed call sites:
- `chatlog_scanner.list_recent_lead_sessions` — lists the encoded dir directly.
- `remote/notify.list_recent_lead_sessions` — same fix, mobile picker.
- `spawn_engine._resume_uuid_matches_cwd` — checks `<uuid>.jsonl` exists inside the encoded dir directly (no more glob-then-decode).
- `user_actions._on_resume_clicked` + `remote/api.resume_lead` — now call `_resume_uuid_matches_cwd` **before** `orch.close(LEAD)`, not after. Previously a mismatched uuid was only caught inside `spawn()`, by which point `close()` had already torn the pane down — user left with no Lead pane AND a rejected resume.

`decode_project_dir()` itself is untouched — still used for display/search only (`iter_session_files`'s substring filter), as instructed.

**Tests:** `tests/test_resume_session_picker.py` — new `TestSurvivesLossyPathChars` (parametrized hyphen/underscore/dot/space cwd, using ordinary `tmp_path` instead of the old `hyphen_free_root` fixture — proves the fix no longer needs a hyphen-free root at all), `TestApiResumeLead::test_mismatched_uuid_rejected_before_close`. `tests/test_resume_button_feedback.py` — new `test_mismatched_uuid_rejected_before_close`. Fixed the test suite's own `_encode_cwd()` helper, which only encoded `/` (not `_`/`.`/space) — it was silently piggy-backing on the old lossy decode contract and would have masked the real fix; now delegates to `token_meter.encode_path_for_claude`.

---

## H1 (HIGH, breaks-mac + both-OS hang) — non-claude panes skip env defaults

**Fixed.** `_apply_mcp_timeout` / `_apply_non_interactive_env` / `_apply_color_term` moved from an explicit call site in `spawn_engine.py`'s claude branch (which ran *after* the shell/codex/gemini branches had already early-returned) into `pane_env.py`'s `_build_pane_env()` / `_build_lead_env()` themselves. Every branch that builds its env via either function now gets all three for free — no per-branch call site to forget.

**Tests:** `tests/test_h1_nonclaude_env.py` (new) — spawns codex/gemini/shell end-to-end (real `_build_pane_env()`, not mocked out) and asserts `COLORTERM`/`TERM`/`npm_config_yes`/`GIT_TERMINAL_PROMPT`/`MCP_TOOL_TIMEOUT` all land in the captured spawn env. Updated `test_color_term.py`/`test_non_interactive_env.py` — two pre-existing tests asserted the *old* buggy contract (host env var absent from `_build_pane_env()`'s output); rewrote them to assert the new integrated defaulting instead of reverting the fix.

---

## L5 (risky) — 5-min auto-resume cwd compare is a raw string

**Fixed.** Added `spawn_engine._normalize_cwd_for_compare(cwd)` (`Path.resolve()` + `os.path.normcase()`, falls back to a normcase'd raw string on `OSError`) and used it for both sides of the `prior_uuid_cwd == spawn_cwd` check. Windows case-insensitivity, mixed separators, trailing slashes, or POSIX symlink variants no longer silently disable `--resume` recovery for what is genuinely the same directory. `orchestrator.py:2033` (`consume_session_report`'s raw `cwd` stamp) needed no change — normalizing symmetrically at the single comparison site covers values from either origin.

**Tests:** `tests/test_orchestrator_session_uuid.py` — new `TestRespawnCwdNormalization` (trailing-slash cross-platform; case-difference Windows-only via `skipif`).

---

## L6 (risky) — `config.lead_cwd()` can return a relative path

**Fixed.** All three return points in `lead_cwd()` (explicit `lead` key, common-parent-of-paths, first-listed-path) now go through a local `_absolutize()` (`Path(p).expanduser().resolve()`, falls back to the raw string on `OSError`). A relative configured path handed straight to the native spawn call used to resolve against the *cockpit process's* cwd, not the project.

**Tests:** `tests/test_config.py` — new `TestLeadCwd` (5 tests: explicit-key relative→absolute, already-absolute stays resolved, first-listed-path branch, common-parent branch, no-project still returns `None`).

---

## L2 (risky/breaks-mac) — `gemini_helper._default_agy_paths()` Windows-only fallback

**Fixed.** Added mac candidates gated by `sys.platform == "darwin"`: the Antigravity `.app` bundle's CLI shim (`/Applications/Antigravity.app/Contents/MacOS/agy`) and both common Homebrew prefixes (`/opt/homebrew/bin/agy` Apple Silicon, `/usr/local/bin/agy` Intel), plus `~/.local/bin/agy`. Windows branch unchanged. Bare Linux still relies on PATH alone — audit only flagged mac, no latent-risk claim invented for Linux.

**Tests:** `tests/test_gemini_helper.py` — new `TestDefaultAgyPaths` (windows/mac/linux branches).

---

## L3 (risky/breaks-mac, low) — `pane_env.py` allowlist missing `TMPDIR`/`XDG_*`

**Fixed.** Added `TMPDIR` (POSIX equivalent of Windows' `TEMP`/`TMP`, already allowlisted) and `XDG_CACHE_HOME`/`XDG_CONFIG_HOME`/`XDG_DATA_HOME`/`XDG_STATE_HOME`/`XDG_RUNTIME_DIR` to `_PANE_ENV_ALLOWLIST`.

**Tests:** `tests/test_orchestrator_env_allowlist.py` — new `test_build_pane_env_includes_tmpdir` + parametrized `test_build_pane_env_includes_xdg_vars`.

---

## L4 (cosmetic/breaks-win) — `codex_helper.find_codex_executable` doesn't avoid `.cmd`

**Verified, not guessed, then fixed.** Confirmed on this machine that `@openai/codex`'s npm package vendors a real native `codex.exe` (nested platform-specific optional-dependency package: `node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe`) — same shape `find_claude_executable` already exploits for claude. `find_codex_executable` now mirrors that: on Windows, resolves `shutil.which("codex")` (→ the `.cmd` shim), then checks for the vendored `.exe` relative to the shim's directory and prefers it; falls back to the `.cmd` path (console flash, but still works) if the vendored exe isn't there (e.g. an older codex release).

**Tests:** `tests/test_codex_helper.py` — new `TestFindCodexExecutableAvoidsCmdShim` (prefers vendored exe when present, falls back to `.cmd` when absent, never probes on non-Windows).

---

## L1 (risky/breaks-win, latent) — winpty + spaced binary paths

**Repro'd live on this machine (not guessed) — real bug, root cause fixed, not just the shell-branch workaround.**

Reproduced the failure directly against `_pty_backend.spawn_pty` using this machine's real spaced PowerShell path (`C:\Program Files\PowerShell\7\pwsh.EXE`, confirmed via `shutil.which`):

```
FileNotFoundError: The command was not found or was not executable: "C:\Program Files\PowerShell\7\pwsh.EXE".
```

**Root cause (not what the original workaround comment claimed):** `_WinptyBackend.spawn` pre-joined `argv` into a single cmdline string via `subprocess.list2cmdline(list(argv))`, quoting a spaced `argv[0]`. pywinpty's own `PtyProcess.spawn()` re-splits a *string* argv via `shlex.split(argv, posix=False)` — which does **not** strip quote characters — before its own `shutil.which(argv[0])` existence check. The quoted path came back out of that re-split *still wearing its quotes*, `which()` looked for a file literally named with quote characters in it, found nothing, and pywinpty raised before ConPTY ever started. This is a latent bug for **every** provider branch (claude/codex/gemini), not just shell — e.g. `find_claude_executable`'s own Windows fallback candidates include `C:/Program Files/nodejs`.

**Fix (unify at the root, not per-branch):** `_WinptyBackend.spawn` now passes `argv` as a **list** to `winpty.PtyProcess.spawn()`, never a pre-joined string — this is the same shape `_PosixBackend.spawn` already used. Passing a list skips pywinpty's buggy string-reparsing branch entirely: it takes `argv[0]` verbatim (no quotes to strip) and quotes `argv[1:]` itself via its own internal `list2cmdline` call, so remaining-arg quoting still happens, just exactly once instead of twice.

Re-ran the same repro after the fix — spawns and reads real output (`HELLO_FROM_PWSH`) successfully.

The shell branch's existing basename+PATH workaround is now provably redundant (the backend handles full spaced paths fine) but was left in place unchanged — it still works correctly and removing it would be a scope-expanding behavior change to already-working code for zero functional gain. Comment updated to explain why it's no longer load-bearing.

**Tests:** `tests/test_pty_backend_spaced_path.py` (new) — mocks the `winpty` module to assert argv reaches `PtyProcess.spawn()` as a list with an unquoted `argv[0]`, directly and through the public `spawn_pty()` entrypoint, plus the ConPTY→WinPTY exception-fallback path.

---

## F1 (MEDIUM, security-boundary regression from C1) — path traversal via unvalidated `session_uuid`

**Fixed.** Reviewer found (`docs/reviews/2026-07-10-xplatform-REVIEW.md` F1) that the C1 rewrite of `_resume_uuid_matches_cwd` swapped `base.glob(f"*/{session_uuid}.jsonl")` (traversal-safe — glob treats a `..` segment as a literal child name, not a real dir) for `(proj_dir / f"{session_uuid}.jsonl").is_file()` (traversal-**capable** — `Path.is_file()` follows `..`). Since `session_uuid` reaches this helper from an unvalidated remote request (`remote/api.py::resume_lead` only does `.strip()`; `remote/http_server.py` passes the raw JSON field through), a crafted `session_uuid = "../<other-encoded-dir>/<real-uuid>"` could resolve outside the caller's own project dir and turn the "forgery-proof" check into a filesystem existence oracle — with `orch.close(lead)` already run by the time `spawn()` would separately reject it.

**Fix (reject at the shared helper, before any filesystem use):** added `_SAFE_SESSION_UUID_RE = re.compile(r"^[0-9A-Za-z_-]+$")` in `spawn_engine.py` and a guard at the top of `_resume_uuid_matches_cwd` — any `session_uuid` not matching that charset (covers `/`, `\`, `..`, and anything else outside claude's own uuid/shard-id alphabet) returns `False` immediately, before `session_project_dir_for_cwd` or the `.jsonl` join ever runs. Fixed once in the shared helper covers **both** call sites (desktop `user_actions._on_resume_clicked` and remote `remote/api.resume_lead`) — no per-caller duplication needed.

**`remote/api.resume_lead` defense-in-depth already satisfied structurally:** it already calls `_resume_uuid_matches_cwd` and raises `RemoteApiError(409, ...)` **before** `orch.close(LEAD.name, ...)` runs (see C1 section above) — so the shared-helper fix rejects a traversal payload at that call site before any teardown happens, with no separate guard needed in `resume_lead` itself.

**Tests:** `tests/test_resume_session_picker.py::TestResumeUuidMatchesCwd` — new `test_false_for_path_traversal_uuid_even_when_target_jsonl_exists` (plants a real `.jsonl` under a *different* project's encoded dir, proves a `../<that-dir>/<uuid>`-shaped value still returns `False`), parametrized `test_false_for_any_uuid_with_path_separators_or_dotdot` (`../evil`, `a/b`, `a\b`, `..`, `foo/../bar`, `trailing/`), and `test_normal_uuid_still_passes` (hyphen/underscore uuid regression guard — the new charset check must not reject ordinary ids). Existing `TestResumeUuidMatchesCwd`/`TestSurvivesLossyPathChars`/`TestSpawnResumeUuid`/`TestApiResumeLead` cases untouched and still green.

---

## Verification

- `python -m ruff check` — clean on every touched file.
- `python -m ruff format --check` — clean (5 test files auto-reformatted, re-verified green after; F1 fix files already formatted).
- `lint-imports` — **18/18 contracts kept** (re-verified after F1 fix).
- Targeted pytest across every touched area (resume picker incl. F1 path-traversal regressions, resume button, remote-bridge autofire/repro, color-term, non-interactive-env, MCP timeout, H1 non-claude env, config/lead_cwd, session-uuid resume/normalization, gemini/codex helpers, pane-env allowlist, pty-backend spaced-path, spawn-gate, spawn-codex-argv, launch-session, provider-config) — **all green**.
- Full suite intentionally **not** run — targeted-tests-only project policy; batch full-suite run belongs at the QA gate.
