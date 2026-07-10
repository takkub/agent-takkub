# Cross-platform audit fixes — doctor / plugins group (backend#2)

**Date:** 2026-07-10
**Scope:** M1, M3, M2 from `docs/reviews/2026-07-10-xplatform-CONSOLIDATED.md`. Files touched: `doctor.py`, `plugin_installer.py`, `lead_context.py`, `pane_tools_dialog.py` (+ tests). No commits made (Lead commits after QA gate).

---

## M1 — `doctor.py` auth check (CONFIRMED → fixed)

**Before:** `doctor.py:112,140` checked `~/.claude/credentials.json` (no leading dot) — the real file is `.credentials.json`. Windows branch always SKIPped (harmless-but-useless), POSIX branch always false-WARNed even when logged in. macOS had no Keychain probe at all — real creds live in the login Keychain (service `Claude Code-credentials`), not a file.

**Fix:** rewrote the `# authenticated` block in `check_claude()` as three explicit branches:
- **darwin:** probe the Keychain first via `limit_status._read_keychain_credentials()` (reused, not duplicated — same helper `fetch_usage()` already uses) → OK. Falls back to `.credentials.json` (some machines still have a token-bearing file) → OK/WARN. Neither present → WARN.
- **win32:** `.credentials.json` present → OK/WARN(unreadable); absent → SKIP (Windows Credential Manager isn't directly checkable, unchanged behavior from before, just correct filename).
- **posix (Linux):** `.credentials.json` present → OK/WARN(unreadable); absent → WARN.

**Test:** `tests/test_doctor.py::TestCheckClaudeAuthenticated` (7 cases) — darwin keychain-present/absent+no-file/absent+file-present, posix dotfile present/missing, windows dotfile present/missing. One gotcha found and worked around: spoofing `sys.platform = "darwin"` via `patch("agent_takkub.doctor.sys.platform", ...)` patches the *real* global `sys` module (not a doctor-local copy), so the darwin-only `limit_status.py` module's `urllib.request` import (which lazily imports macOS-only `_scproxy`) blows up on Windows if `limit_status` hasn't been imported yet under the real platform. Fixed by importing `agent_takkub.limit_status` once before the platform spoof in each darwin test (caches the module so the darwin branch of `urllib.request`'s own import never re-fires).

---

## M3 — `plugin_installer.py` bare `claude` + missing `CLAUDE_CONFIG_DIR` (PLAUSIBLE → verified real → fixed)

**Verify:** confirmed `_claude()` (line 102 pre-fix) called `subprocess.run(["claude", *args], ..., shell=False, ...)` — a bare name, no `env=`. On Windows, `which("claude")`-style PATH resolution for a bare `"claude"` under `shell=False` needs the OS loader to find `claude.exe`/`claude.cmd`/`claude.bat` via `PATHEXT`; when only the npm `claude.cmd` shim exists (common — see `config.find_claude_executable()`'s own docstring, which exists specifically to dodge this), `CreateProcess`-style bare-name resolution without `shell=True` fails outright (`FileNotFoundError`) — this is exactly the failure mode `find_claude_executable()` already exists to avoid everywhere else in the codebase (spawn_engine, project_rules, claude_update). `plugin_installer.py` was the one caller that never used it. Confirmed real, not just plausible.

**Fix:**
- `_claude()` now resolves the executable via `config.find_claude_executable()` (lazy import, matches the module's existing local-import style) instead of the bare string `"claude"`.
- New `_claude_env()` helper builds `env=` = the GUI process's own `os.environ` + `CLAUDE_CONFIG_DIR` set via `setdefault` — an existing override in the calling process's env wins (a session-specific profile), otherwise defaults to `config.default_claude_config_dir()` (plain `~/.claude` for a dev checkout, the isolated `DATA_HOME/claude-config` for an installed build). Passed to `subprocess.run(..., env=_claude_env())`.

**Test:** `tests/test_plugin_installer.py` — 4 new cases: resolved-exe-used-not-bare-name, `_claude_env` sets `CLAUDE_CONFIG_DIR` when unset, preserves an existing override, and `_claude()` actually passes `env=` through to `subprocess.run`. All 8 pre-existing tests (which mock `_claude` wholesale) still pass unchanged.

---

## M2 — hardcoded `~/.claude/plugins/` (PLAUSIBLE → verified per-site → 2 of 3 sites fixed, 1 unaffected)

Verified each of the three cited sites individually, per the task's "some may be intentional dev-mode reads" caveat:

1. **`lead_context.py:640` (`_default_plugin_dirs`) — bug, fixed.** This resolves the `--plugin-dir` list every spawned pane inherits. An installed build's default profile is `config.default_claude_config_dir()` = `DATA_HOME/claude-config`, NOT `~/.claude` — so the old hardcode meant an installed cockpit's plugin injection silently looked in the wrong dir (a dev checkout on the same machine's `~/.claude`) whenever a pane used the default profile. Fixed: `cache = default_claude_config_dir() / "plugins" / "cache"`. Caller (`spawn_engine.py:1540`, out of scope for this task — owned by another instance) passes no `project`, so this only fixes the *instance-default* profile case; a project with its own explicit profile override (`pane_env.inject_user_profile_env`, also out of scope) still isn't reflected in `--plugin-dir` — flagged, not fixed here (would need `project` threaded through `_default_plugin_dirs`, which touches `spawn_engine.py`).

2. **`pane_tools_dialog.py:56` (`_PLUGINS_INSTALLED_FILE`, backing `discover_marketplace_plugins`/`discover_marketplaces`) — bug, fixed.** This is the 🧩 Plugins matrix's "what's actually installed" registry read — same class of bug as #1 (installed build reads the wrong profile's registry). Fixed: replaced the module-level constant (computed once at import, using `pathlib.Path.home()` directly) with a `_default_plugins_installed_file()` function that resolves `config.default_claude_config_dir() / "plugins" / "installed_plugins.json"` fresh on every call (mirrors `config.default_claude_config_dir()`'s own "computed fresh so tests can monkeypatch" doc-note). Both `discover_marketplace_plugins`/`discover_marketplaces` default params changed from the stale module constant to `None`, resolved lazily inside the function body — existing callers that pass `installed_file` explicitly (all current tests, and the one production call site `discover_marketplaces()` at line ~561) are unaffected either way.

3. **`plugin_installer.py:125` (`installed_on_disk`) — NOT a bug, left as-is.** This function takes `home: Path | None = None` as an explicit parameter (`base = (home or pathlib.Path.home()) / ".claude" / "plugins" / "cache"`) — callers that care about profile isolation already have the hook to pass a different `home`. Checked every call site: `missing_plugins()` (module-internal, also defaults to real home) and the GUI's install-thread caller in `user_actions.py` (out of scope) — neither currently overrides `home`, so this has the *same* practical effect as the two bugs above for an installed build, but the difference is architectural: the function already exposes the correct extension point, so "fixing" it here would mean changing the *default value* of an already-parameterized function, which is exactly what #2 just did for `pane_tools_dialog`. Left alone per the task's file scope (`plugin_installer.py`'s `installed_on_disk`/`missing_plugins` weren't named in the M2 finding — M3's `_claude()`/`_claude_env()` were the assigned fix for this file) — flagging here so a follow-up can apply the identical `home=None → default_claude_config_dir()` default-swap if desired.

**Test:**
- `tests/test_plugin_policy.py` (existing, `_default_plugin_dirs`) — reran unchanged; still green modulo 2 **pre-existing** failures (`test_teammate_gets_superpowers_and_pordee_not_addy`, `test_design_roles_get_ui_ux_pro_max`) confirmed via `git stash`/re-run to fail identically on the unmodified baseline — caused by this dev machine's real `~/.takkub/pane-tools.json` role-policy override (an environmental artifact, not a regression from this change; not touched/fixed here as it's outside the M1/M2/M3 scope).
- `tests/test_pane_tools_dialog.py` — 3 new cases: `_default_plugins_installed_file()` resolves via the config helper, and both `discover_marketplace_plugins()`/`discover_marketplaces()` called with **no argument** pick it up.

---

## Verification run

```
pytest tests/test_doctor.py tests/test_doctor_thread_async.py tests/test_doctor_version.py \
       tests/test_plugin_installer.py tests/test_pane_tools_dialog.py tests/test_plugin_policy.py \
       tests/test_cli_pane_tools.py
→ 144 passed, 2 failed (pre-existing, environmental — see M2 note above)

ruff check <touched files>          → all clean
ruff format <touched files>         → 2 reformatted (pane_tools_dialog.py, test_doctor.py), reran tests green
lint-imports                        → 18 kept, 0 broken
```

No `spawn_engine.py` / `config.py` / `chatlog_scanner.py` / `remote/*` / `pane_env.py` touched (owned by other instances this session).

---

## F2 — `plugin_installer.installed_on_disk` hardcoded `~/.claude` broke installed builds (fix-loop, CONFIRMED)

**Reported by:** reviewer (`docs/reviews/2026-07-10-xplatform-REVIEW.md`, finding F2), correcting M2 item 3 above — that item concluded "not a bug, left as-is" reasoning the `home` param was the correct extension point; the reviewer found the *default* itself (no override) is what actually breaks, since no production call site ever passes `home`.

**Confirmed root cause:** `installed_on_disk()`'s default base was `(home or Path.home()) / ".claude" / "plugins" / "cache"`, while `_claude_env()` (the M3 fix above) makes `claude plugin install` write into `config.default_claude_config_dir() / "plugins" / "cache"`. For an installed build, `default_claude_config_dir()` = `DATA_HOME/claude-config` ≠ `~/.claude` — so a real install lands in `DATA_HOME/claude-config/plugins/cache`, but `installed_on_disk()` (called with no `home` from both `install_plugin()`'s post-install check and `missing_plugins()`) kept reading `~/.claude/plugins/cache`. Result: `install_plugin()` sees exit 0 but "not found on disk" → returns `False`, and `missing_plugins()` re-lists the plugin as missing forever → the Plugins dialog loops re-prompting an already-installed plugin. Dev checkouts were unaffected (`default_claude_config_dir() == ~/.claude` there), which is why this shipped unnoticed.

**Fix:** `installed_on_disk()` now resolves its default base via `config.default_claude_config_dir() / "plugins" / "cache"` when `home` is not given, matching `_claude_env()`'s install target exactly. The `home` param is kept (still overrides directly) purely so existing tests can point at a `tmp_path` sandbox — no production caller passes it.

**Test:** `tests/test_plugin_installer.py::test_installed_on_disk_matches_install_target_when_data_home_differs` — monkeypatches `config.default_claude_config_dir()` to an isolated dir (simulating an installed build), asserts `_claude_env()`'s `CLAUDE_CONFIG_DIR` and `installed_on_disk()`'s read base agree, and that a decoy `~/.claude` cache (the pre-fix hardcoded location) is correctly ignored.

**Verification run:**
```
pytest tests/test_plugin_installer.py → 13 passed
```
No commit made (Lead commits after QA gate).
