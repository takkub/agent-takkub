# Isolation Plan Cross-Check (Codex)

Date: 2026-07-05  
Role: adversarial reviewer  
Scope: read-only cross-check of the dev/prod isolation plan A-D, with code evidence from the current working tree.

## Executive Findings

Severity order:

1. **HIGH - CLI_BIN_DIR is not sufficient for npm-installed panes if no real console script exists in the venv bin/Scripts dir.** The npm global `takkub` launcher is under the npm package's `npm/bin/*.js`, while `config.CLI_BIN_DIR` resolves installed mode to `Path(sys.executable).parent` (`~/.agent-takkub/venv/Scripts` or `bin`). That only works if the wheel's `[project.scripts]` entry points are installed and remain usable. The npm wrapper itself does not live there and `postinstall.js` invokes `python -m pip install` into the venv, so the test gate must assert both `venv/Scripts/takkub(.exe)`/`venv/bin/takkub` and the npm wrapper path behavior. If a wheel reinstall skips script generation or the app is launched through `pythonw.exe` with a mismatched executable, panes can still resolve a different `takkub` from PATH. This breaks `takkub _hook`, `send`, and `done` even after A stamps `TAKKUB_PORT_FILE`.

2. **HIGH - per-DATA_HOME lock does not isolate default Claude state.** `user_profile.py` intentionally leaves the default profile at `~/.claude`, and `inject_user_profile_env()` only sets `CLAUDE_CONFIG_DIR` for non-default profiles. Therefore dev and prod can run concurrently with different DATA_HOME but the same default Claude session/plugin/config surface. Even if `--mcp-config` is cockpit-managed, default Claude config can still affect auth/session history/plugin behavior. This is a cross-instance shared resource not covered by C.

3. **HIGH - the plan trusts `--strict-mcp-config` / role variants more than current docs allow.** `docs/INSTALL.md` says current Claude versions do not fully block user-level `~/.claude.json` MCP loading with `--strict-mcp-config`; `shared_dev_tools.py` comments and CLAUDE.md state the stronger opposite. If true in the field, prod and dev can still see user-level MCPs, including shared browser/server configs, regardless of DATA_HOME. This is exactly the kind of "pane talks to wrong outside resource" isolation failure C does not address.

4. **MED-HIGH - ASSETS_ROOT build_py staging misses non-wheel and editable paths.** `setup.py` stages `_assets` only during `build_py`, then deletes it. That is acceptable for a normal wheel build, but fragile for `sdist`, editable installs, direct `python -m agent_takkub` from an installed editable layout, build frontends that run metadata without `build_py`, or wheels built from an sdist that does not include root `CLAUDE.md` / `.claude/agents`. Since `_assets` is not committed and is removed after build, any installed-mode runtime that did not pass through that exact wheel `build_py` path will have `ASSETS_ROOT` pointing at a missing directory.

5. **MED-HIGH - restart/update still deletes the static `PORT_FILE`, not the effective `_get_port_file()`.** `update_panel.py` unlinks imported `PORT_FILE` before restart/update. In multi-instance mode `TAKKUB_PORT_FILE` can point elsewhere, and after a per-DATA_HOME lock change the effective port file is the isolation boundary. Deleting only `config.PORT_FILE` can leave a stale per-PID or custom port file behind. A successor pane that inherits or resolves that path may connect to a stale/wrong server.

6. **MED - browser profile isolation is DATA_HOME-scoped but only by project/role/shard, not by cockpit instance identity.** `browser_profile_mcp_config_path()` writes profiles under `RUNTIME_DIR/browser-profiles/<project>-<role>...`. If two instances intentionally share DATA_HOME, they will still share browser profiles and generated MCP files. Per-DATA_HOME lock prevents concurrent same-DATA_HOME cockpits unless `TAKKUB_ALLOW_MULTI=1`, but dev/test multi mode explicitly skips locking and can still collide.

7. **MED - `TAKKUB_ALLOW_MULTI` semantics conflict with per-DATA_HOME locking unless redefined.** Today `TAKKUB_ALLOW_MULTI=1` skips the single-instance lock and sets a per-PID `TAKKUB_PORT_FILE`. If C changes the lock to per-DATA_HOME but keeps `ALLOW_MULTI` as "skip all locking", dev/prod tests will not exercise the new lock. If it changes to "allow different DATA_HOME only", existing tests that rely on unrestricted multi will change behavior. This needs explicit compatibility tests.

8. **MED - CLI auth prevents many cross-instance sends, but hook/done failure mode remains hard-to-debug if the wrong binary talks to the right port or vice versa.** `cli_server.py` binds `done/send/hook` to `TAKKUB_PANE_TOKEN` and Lead commands to `TAKKUB_LEAD_TOKEN`, which is good. But if PATH resolves a stale CLI from another code version, it may read the stamped port file and reach the right server with missing/new fields, or read the wrong DATA_HOME if the stamped env is absent in a non-pane subprocess. Version skew can produce false "unauthorized" or silent hook loss rather than clean isolation.

## Evidence and Adversarial Analysis

### B. ASSETS_ROOT and CLI_BIN_DIR

Current implementation:

- `config.REPO_ROOT = Path(__file__).resolve().parents[2]`.
- Installed `ASSETS_ROOT` becomes `Path(__file__).resolve().parent / "_assets"`.
- Installed `CLI_BIN_DIR` becomes `Path(sys.executable).resolve().parent`.
- `setup.py` creates `src/agent_takkub/_assets`, copies root `CLAUDE.md` and `.claude/agents/*.md`, runs `build_py`, then deletes `_assets`.
- `pyproject.toml` includes `_assets/CLAUDE.md` and `_assets/.claude/agents/*.md` as package data.

Blind spots:

- **sdist:** `setup.py` does not customize `sdist`; if the npm package ever ships an sdist or CI builds a wheel from an sdist, root `.claude/agents` may be omitted unless MANIFEST/setuptools includes it. The package-data entries point to staged files that do not exist outside `build_py`.
- **editable install:** an editable install may not materialize package data through `build_py`; because `_assets` is deleted, installed-mode detection plus editable layout can point to missing assets. Editable source mode may be okay if `DATA_HOME == REPO_ROOT`, but an editable install with `AGENT_TAKKUB_HOME` set forces installed-style DATA_HOME while code still lives in the repo.
- **build from cwd other than repo root:** `_ROOT = Path(__file__).resolve().parent` is robust if `setup.py` is present, but a PEP 517 frontend can run metadata/build phases where root assets are not available in an sdist extraction.
- **empty agents dir:** `_stage_assets()` creates `_assets/.claude/agents` but does not fail if the source `.claude/agents` directory is missing or empty; the wheel can build with no roles and only fail at runtime.
- **macOS launcher layout:** `sys.executable.parent` is correct for a venv launched by `bin/python`, but a GUI `.app`/shortcut or npm wrapper may launch via `pythonw`/stub where the executable parent is not the console script dir expected by shells. On Windows `pythonw.exe` and `takkub.exe` are both in `Scripts`, but this should be asserted because the npm helper has a `venvPythonw()` concept while `agent-takkub.js` currently uses `venvPythonIfExists()` (`python.exe`).
- **npm wrapper mismatch:** npm's global `takkub` command is a Node script that runs `venv python -m agent_takkub.cli`. Panes do not get the npm package's bin dir first; they get `CLI_BIN_DIR`. That is only safe if the wheel's console script `takkub` exists in the venv. The prod gate must assert this exact PATH-prepend behavior, not just import/config resolution.

Recommended test additions:

- Build wheel from repo, install into temp venv, assert `_assets/CLAUDE.md` and at least expected role files exist via `importlib.resources`.
- Build sdist then wheel from sdist in a temp dir, install, assert the same.
- `pip install -e .` with `AGENT_TAKKUB_HOME` set to temp, assert config mode and asset resolution are intentional.
- On Windows and macOS, assert `Path(sys.executable).parent / ("takkub.exe" or "takkub")` exists and that running it reads the same `agent_takkub.__file__`.
- Assert spawned pane env PATH's first entry is the expected CLI dir and `takkub _hook` resolves to that executable/script.

### C. Is per-DATA_HOME lock enough?

No. It fixes one destructive global lock, but not every shared mutable surface.

Resources still global or conditionally shared:

- **Default Claude config/auth/session state:** default profile uses `~/.claude` (`user_profile.py`); only non-default project profiles set `CLAUDE_CONFIG_DIR`. Concurrent dev/prod default-profile panes can share Claude session history, auth, plugins, and maybe user-level MCPs.
- **User-level MCP config:** docs warn `--strict-mcp-config` may not fully block `~/.claude.json` MCP loading. If true, dev/prod share those MCP definitions regardless of DATA_HOME.
- **Browser MCP external resources:** generated browser profiles are under `RUNTIME_DIR`, so different DATA_HOME is fine, but same DATA_HOME + `TAKKUB_ALLOW_MULTI=1` shares them. `mb` remains hardcoded to CDP 9222 per CLAUDE.md, so any concurrent mb usage still controls one Chrome.
- **Qt WebEngine cache/profile:** no explicit `QTWEBENGINE_CHROMIUM_FLAGS` profile/cache path is set. QtWebEngine may default to organization/application-name scoped user cache/storage. Two processes with same app name can share disk cache/GPU process state even with different DATA_HOME. This is not obviously fatal, but it is a global resource to verify.
- **Settings split:** installed settings are under DATA_HOME; dev settings are `~/.takkub` by design. That isolates dev/prod, but multiple dev checkouts still share `~/.takkub`. If "dev/prod isolation" also means two dev trees, C does not cover it.
- **Worktree storage:** `worktree_manager` stores isolated worktrees under `<DATA_HOME>/worktrees/<project>`. Different DATA_HOME is fine; same DATA_HOME multi is not.
- **Shared logs/bootstrap paths:** `app.py` boot log uses `Path(__file__).resolve().parents[2] / "runtime" / "boot.log"` before config import. In installed mode this resolves under the venv/package ancestor, not necessarily DATA_HOME. That can make diagnostics land outside the intended per-instance data root.

Recommended C hardening:

- Define a true `INSTANCE_ID` or `INSTANCE_HOME` and include it in lock path, window title, port file, browser profile root, QtWebEngine cache path, and generated MCP filenames.
- For installed prod, consider setting `CLAUDE_CONFIG_DIR` to a prod-scoped default profile unless the user explicitly selects shared default.
- Add a startup audit log listing DATA_HOME, SETTINGS_HOME, ASSETS_ROOT, CLI_BIN_DIR, effective port file, CLAUDE_CONFIG_DIR/default, MCP config path, browser profile root, and QtWebEngine cache/storage path if discoverable.

### `TAKKUB_ALLOW_MULTI`, restart successor, watchdog, auto-kill

Risk interactions:

- **ALLOW_MULTI currently bypasses lock entirely.** With per-DATA_HOME lock, decide whether it means "allow multiple DATA_HOME" or "skip lock". Tests currently set it to avoid global lock kill behavior. Keeping skip-all-lock lets same-DATA_HOME instances still collide on runtime, profiles, shared-mcp, snapshots, and port files.
- **Restart successor waits on the lock.** `TAKKUB_RESTART_SUCCESSOR` calls `_wait_predecessor_exit(_instance_lock)`. If the lock path is keyed by DATA_HOME, the successor must compute the same DATA_HOME as the predecessor before waiting. If launched through npm/global wrapper with different `AGENT_TAKKUB_HOME` env propagation, it may wait on a different lock and open concurrently.
- **Auto-kill scope changes.** The current global lock kill intentionally kills the whole old process tree. With per-DATA_HOME lock, auto-kill should only target predecessor holding the same DATA_HOME lock. But the psutil kill tree still kills all children of that PID; safe only if lock key cannot alias across dev/prod.
- **Port-file deletion in restart/update uses static `PORT_FILE`.** It should delete `config._get_port_file()` or at least log both. Otherwise custom/per-PID port files survive.
- **Deadman watchdog no longer hard-kills, but docs/task prompt mention old hard-kill behavior.** If any old watchdog/auto-kill remains in tests/docs, validate it cannot kill a different DATA_HOME after lock change.

Recommended tests:

- Start two instances with different `AGENT_TAKKUB_HOME`, no `TAKKUB_ALLOW_MULTI`; assert no kill and distinct lock/port/runtime.
- Start two instances with same `AGENT_TAKKUB_HOME`; assert second waits/kills/dialogs only that instance.
- Restart with custom `AGENT_TAKKUB_HOME` and custom `TAKKUB_PORT_FILE`; assert successor uses the same lock and stale effective port file is removed.
- `TAKKUB_ALLOW_MULTI=1` same DATA_HOME should either be explicitly forbidden or have unique runtime subdirs.

### A and remaining cross-instance pane attack surface

What A fixed well:

- `pane_env._apply_port_file()` stamps effective `TAKKUB_PORT_FILE` into both teammate and Lead envs.
- `cli_server.py` uses `TAKKUB_PANE_TOKEN` for `done`, `send`, and `hook`; it derives `(project, role)` from the server token table instead of trusting request fields.
- Lead-only commands require `TAKKUB_LEAD_TOKEN`.

Remaining risks:

- **Wrong CLI binary with right port file:** PATH can still resolve a stale `takkub` if `CLI_BIN_DIR` is wrong or missing a console script. It will read `TAKKUB_PORT_FILE`, reach the right server, then fail by protocol/version skew.
- **Right CLI binary with wrong/missing port file:** non-pane subprocesses or hooks that do not inherit pane env still fall back to their own `DATA_HOME/runtime/port`. `HOOK_COMMAND` is just `takkub _hook`; it depends completely on PATH + env.
- **Lead token leakage blast radius:** Lead env contains `TAKKUB_LEAD_TOKEN`. A malicious tool/process inside the Lead pane can command the instance. That is intended, but if default Claude config/plugins are shared across prod/dev, a plugin surface can affect both instances.
- **Manual terminal fallback:** CLI without `TAKKUB_ROLE` is allowed and uses active project fallback. This is useful for debugging, but if a human shell has stale `TAKKUB_PORT_FILE`, it can talk to an unintended running cockpit. Tokens limit mutating commands from panes, but manual terminal lead-only commands without token are rejected; read/status/list behavior should still be reviewed for cross-instance confusion.

## Prod-Mode Test Gate Gaps

The proposed D gate should not stop at "install wheel and assert config resolution." It should add adversarial cases:

- Wheel install via npm postinstall path: global Node wrapper -> venv python -> installed package -> pane PATH -> venv console script.
- Wheel-from-sdist, not only wheel-from-repo.
- Editable install with `AGENT_TAKKUB_HOME` override.
- macOS venv `bin/` and Windows `Scripts/` CLI resolution.
- GUI/pythonw startup path if used by shortcut.
- Two-instance integration with different DATA_HOME and same default `~/.claude`.
- `--strict-mcp-config` empirical check: spawn a Claude command with a sentinel user-level MCP in `~/.claude.json` and assert pane does not see it, or downgrade docs/comments if impossible.
- Restart/update with custom `TAKKUB_PORT_FILE` and per-DATA_HOME lock.

## Bottom Line

The plan is directionally right but still has three isolation blind spots that can falsify it in production:

1. Asset/config packaging is wheel-build-path dependent and not yet proven for sdist/editable/npm wrapper paths.
2. Per-DATA_HOME lock isolates cockpit runtime, not the default Claude profile/MCP/plugin/browser surfaces.
3. `TAKKUB_PORT_FILE` fixes server selection only if the pane resolves the correct `takkub` binary and inherits the env in every hook/subprocess path.

Treat D as a full installed-mode behavioral harness, not just import-level assertions.
