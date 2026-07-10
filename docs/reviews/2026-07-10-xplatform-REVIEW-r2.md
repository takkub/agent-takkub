# Cross-Platform Fix Wave — Re-Audit Round 2 (reviewer)

**Date:** 2026-07-10
**Scope:** the *full* cumulative working-tree `git diff` (~30 src+test files) of the cross-platform fix wave, **after** F1/F2 + FU1/FU2 landed on top of round-1's review.
**Method:** read the real code (no guessing) at every cited line; traced each of the 5 focus items end-to-end; ran the security-critical + FU1/F2 regression suites to get evidence, not just assertions.

## Verdict: **CLEAN — no new findings**

All round-1 findings (F1 traversal, F2 installed-dir) are now closed, the two follow-ups (FU1 flag reset, FU2 profile threading) are correct, and no new correctness or security regression surfaced in the parts round 1 didn't examine. Nothing blocks the QA gate.

---

## Focus-item verification

### (1) F1 — `_SAFE_SESSION_UUID_RE` closes traversal at **every** entry, no bypass ✅

`_SAFE_SESSION_UUID_RE = re.compile(r"^[0-9A-Za-z_-]+$")`, checked at the **top** of the shared `_resume_uuid_matches_cwd` (`spawn_engine.py:136`) before any filesystem use. That helper is the single choke point for all three entry paths:

- **desktop** — `user_actions._on_resume_clicked` calls it before `orch.close()` (`user_actions.py:310`).
- **remote** — `remote/api.resume_lead` calls it before `orch.close()` (`api.py:271`); `session_uuid` is `isinstance str` + `.strip()`-validated first (`api.py:263-265`), so a non-str JSON payload can't reach `re.match` and raise.
- **spawn (defense-in-depth)** — `spawn()` re-validates via the same helper (`spawn_engine.py:1681`) before `--resume`.

Bypass attempts, all rejected by the ASCII-only charset:
- **`..` / `/` / `\`** → contain chars outside `[0-9A-Za-z_-]` → `False`. Verified by `test_false_for_path_traversal_uuid_even_when_target_jsonl_exists` (plants a real `.jsonl` under a *different* project's encoded dir, proves `../<dir>/<uuid>` still returns `False`) + parametrized `../evil, a/b, a\b, .., foo/../bar, trailing/`.
- **unicode** (e.g. Cyrillic `а`) → `[A-Za-z]` is a literal ASCII range, not `\w`; non-ASCII rejected.
- **URL-encoded** (`%2e%2e`) → `%` rejected; nothing URL-decodes `session_uuid` (JSON body, not query string).
- **empty** → `+` needs ≥1 char; remote path also rejects empty via the `.strip()` guard.

**Non-issue noted (not a finding):** `re.match(...$)` matches a value with a single *trailing* `\n` (Python `$` semantics). Not exploitable — the remote path `.strip()`s it away, the desktop path is a trusted picker value, and even if a `\n` slipped through it only yields a filename `uuid\n.jsonl` (no traversal, `is_file()` just returns `False`). `re.fullmatch` would be marginally cleaner but changes nothing observable. **49/49 `test_resume_session_picker.py` green.**

### (2) F2 — install write dir and `installed_on_disk` read dir now agree ✅

`_claude_env()` sets `CLAUDE_CONFIG_DIR = default_claude_config_dir()` (write target), and `installed_on_disk(home=None)` now resolves its base from the *same* `default_claude_config_dir() / "plugins" / "cache"` (`plugin_installer.py`). For the real installed build (`DATA_HOME != REPO_ROOT`) both resolve to `DATA_HOME/claude-config` — the "CLI success but not on disk" loop is closed. `test_installed_on_disk_matches_install_target_when_data_home_differs` pins it with a decoy `~/.claude`. **13/13 `test_plugin_installer.py` green.**

*Residual edge, deliberately not flagged:* if the GUI process is itself launched with an explicit `CLAUDE_CONFIG_DIR` ≠ `default_claude_config_dir()`, `setdefault` writes there while `installed_on_disk` still reads the default. Contrived (the cockpit doesn't set that on itself; a bare launch has no such var), and no worse than pre-diff behaviour. Out of scope for this fix.

### (3) FU1 — `last_spawn_resumed = False` reset is at the right site, never clobbers a legitimate `True` ✅

The reset (`spawn_engine.py:833`) lives inside `_launch_session`, which is the **non-claude common tail** — called only by shell (`:1055`), gemini/agy (`:1135`), codex (`:1206`). The claude branch does **not** route through `_launch_session`; it sets its own `last_spawn_resumed = resumed` at `:1792` on a separate path. So the reset only ever fires for providers that have no `--resume` concept, where `False` is unconditionally correct — it can never overwrite the `True` a real claude `--resume` just set. Same `_exit_key(project_ns, role_name)` used at the set site (1792), reset site (833), and `_auto_respawn` read site (2141), so the flag the respawn reads is the one this spawn wrote. The exact provider-substitution scenario (claude-substitute set `True`, real codex respawns, later crash still replays) is covered by `test_provider_self_update_exit_replays_task`.

### (4) FU2 — no cross-project profile leak ✅

`_default_plugin_dirs(role, project=project_ns)` resolves the cache via `user_profile.config_dir_for(project)` — a **pure function** of the project name (`profile_for(project)` → registry lookup → `Path`), with `out` as a local list. No module-level mutable state keyed on project, so one project's custom profile cannot bleed into another's `--plugin-dir`. `project=None` (doctor/smoke callers) keeps byte-identical `default_claude_config_dir()` behaviour. Covered by `test_different_project_default_profile_unaffected_by_others_custom`.

### (5) Other diff angles round 1 didn't deeply examine — all clean ✅

- **H1 (no claude regression):** removing the claude branch's explicit `_apply_*` calls is safe — **all four** branches build env via `_build_pane_env()`/`_build_lead_env()` (`:1047/1114/1164/1384`), which now apply the three helpers internally. Grepped `spawn_engine.py`: **zero** claude-branch overrides of `COLORTERM`/`TERM`/`MCP_TOOL_TIMEOUT`/`npm_config_yes`/`GIT_TERMINAL_PROMPT`, so the order-flip (applied at build time vs. after claude-specific env additions) is observationally inert.
- **L1 (`_pty_backend`):** argv-as-list change is inside `_WinptyBackend` (Windows-only); POSIX backend untouched. Removed `import subprocess` is no longer referenced anywhere but a comment — safe.
- **C1 core:** `chatlog_scanner` + `remote/notify` pickers both now list the exact `session_project_dir_for_cwd(...)` dir and glob `*.jsonl` — identical, consistent shape; `decode_project_dir` retained for display/search only.
- **doctor M1:** correct three-way `darwin`(Keychain-first)/`win32`/`posix` split, `.credentials.json` filename fixed on every branch, darwin import gated by `sys.platform`.
- **config L6:** `_absolutize` applied to all three `lead_cwd()` return points, `OSError` fallback to raw.
- **codex L4 / gemini L2:** vendored-exe probe gated `sys.platform == "win32"`; agy mac candidates gated `darwin` — both keep the other OS's branch intact.

---

## Evidence run (targeted, per project policy — full suite belongs at QA gate)

```
tests/test_resume_session_picker.py            49 passed   (F1 + C1)
tests/test_launch_session.py                              (FU1 reset)
tests/test_orchestrator_auto_respawn_replay.py           (FU1 e2e)
tests/test_plugin_installer.py                 30 passed combined  (F2)
```
All green.
