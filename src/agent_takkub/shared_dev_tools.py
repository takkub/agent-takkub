"""Cockpit-managed shared dev-tool config.

Some dev tools should follow Lead into every project tab without
requiring per-project `.claude/settings.json` edits:

  * **Pordee** — handled at the plugin layer via `_SAFE_PLUGINS` in
    orchestrator.py, no settings file involved.
  * **Browser MCPs** — playwright + chrome-devtools are injected into
    every pane via `runtime/shared-mcp.json` so smoke tests, UX checks,
    and crawls are available from any project's Lead without per-project
    wiring.
  * **User MCPs** — allowlisted entries from the user's own `~/.claude.json`
    mcpServers are merged into `runtime/shared-mcp.json` so every cockpit
    pane inherits them automatically without manual setup. Browser MCPs
    (playwright, chrome-devtools) take precedence on name collision.
    Credential-bearing entries and entries without a `type` field are skipped.
  * **rtk hook** — still per-project (the PreToolUse Bash hook lives
    in `.claude/settings.json`). Use the `⚡ Install rtk` button to add
    it to a specific project.

The shared-mcp.json file lives under `runtime/` (gitignored). The
cockpit writes it at startup via `ensure_browser_mcps()` +
`ensure_user_mcps()` and every claude spawn receives it via `--mcp-config`.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import subprocess
import threading

from ._win_console import SUBPROCESS_NO_WINDOW
from .config import RUNTIME_DIR
from .pane_tools_policy import effective_mcps

_log = logging.getLogger(__name__)

SHARED_MCP_FILE = RUNTIME_DIR / "shared-mcp.json"

# Browser MCPs that the cockpit forces into every pane so smoke tests,
# UX checks, and crawls are available from any project's Lead. These
# are vanilla npx-stdio servers with no auth, so we hard-code their
# config rather than asking the user to wire them up per project.
#
# Why ship them via shared-mcp.json instead of letting claude read
# them from the user's ~/.claude.json:
#   The cockpit launches every pane with `--setting-sources project,local`
#   to dodge claude-obsidian's crashing SessionStart hook. That flag
#   also blocks user-level mcpServers from loading, so even though the
#   user has `playwright` + `chrome-devtools` registered in
#   ~/.claude.json, panes don't see them. Folding the configs into the
#   cockpit's --mcp-config restores them without re-opening the
#   user-level settings can-of-worms.
# Versions pinned 2026-05-17 — the latest tags on npm at the time
# `BROWSER_MCPS` was authored. Pinning matters because `@latest` makes
# npx hit the npm registry on every spawn to resolve the dist-tag,
# which can take long enough on a cold Windows machine to blow past
# claude code's MCP startup window — the server then shows up as
# "not connected" and the user has to retry. With a literal version
# string, npx checks the local cache first and skips the registry
# round-trip when the package is already there.
#
# Bump these when you want to take a new release. Recipe: `npm view
# @playwright/mcp version` and `npm view chrome-devtools-mcp version`,
# update here, ship a commit, then call `ensure_browser_mcps` on next
# boot (it only adds missing names, so an explicit version bump needs
# the old entry to differ from the new desired config — changing the
# version string is enough to trigger an update).
_PLAYWRIGHT_MCP_VERSION = "0.0.75"
_CHROME_DEVTOOLS_MCP_VERSION = "0.26.0"

BROWSER_MCPS: dict = {
    "playwright": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", f"@playwright/mcp@{_PLAYWRIGHT_MCP_VERSION}"],
        "env": {},
    },
    "chrome-devtools": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", f"chrome-devtools-mcp@{_CHROME_DEVTOOLS_MCP_VERSION}"],
        "env": {},
    },
}


def warm_browser_mcps() -> None:
    """Pre-warm the npx cache for the browser MCPs at cockpit boot.

    First-call latency for `npx -y @playwright/mcp@<v>` is high enough
    on a cold Windows machine to blow past claude code's MCP startup
    window, which is the failure mode behind "Playwright MCP ยังไม่
    connect" reports from the user. Pinning the version (already
    shipped) is half the cure; this is the other half — kick each
    server in a background daemon thread so npx has the tarball
    extracted and the entrypoint resolved by the time claude's first
    `mcp__playwright__*` call lands.

    Implementation: spawn each MCP with stdin closed (DEVNULL). The
    server starts, reads EOF on stdin, and exits cleanly within a
    second or two. We don't care about the output — the side effect
    is the npx cache. A 30 s timeout caps the worst case (slow npm
    registry / first download); errors are swallowed so a network
    blip never blocks cockpit boot.

    Daemon threads so cockpit shutdown doesn't wait on them.

    Guarded by TAKKUB_SKIP_MCP_WARM (any truthy value): every Orchestrator()
    construction calls this, so a full pytest run building dozens of
    Orchestrators would otherwise spawn dozens of real `npx @playwright/mcp`
    + `npx chrome-devtools-mcp` processes that outlive individual tests and
    pile up (#91 — CPU idle 0% mid-suite). conftest.py sets the env var for
    every test; the check lives here (not just at the caller) so no import
    path can bypass it.
    """
    if os.environ.get("TAKKUB_SKIP_MCP_WARM", "").strip() not in ("", "0"):
        _log.debug("warm_browser_mcps: skipped (TAKKUB_SKIP_MCP_WARM set)")
        return

    def _warm_one(argv: list[str]) -> None:
        try:
            subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
                creationflags=SUBPROCESS_NO_WINDOW,
            )
        except Exception:
            pass

    for name, cfg in BROWSER_MCPS.items():
        argv = [cfg["command"], *cfg["args"]]
        threading.Thread(
            target=_warm_one,
            args=(argv,),
            name=f"warm-{name}",
            daemon=True,
        ).start()


def shared_mcp_config_path() -> str | None:
    """Absolute path to the shared MCP config file if it exists and has
    at least one MCP server entry. Returned to the orchestrator's argv builder."""
    if not SHARED_MCP_FILE.is_file():
        return None
    try:
        data = json.loads(SHARED_MCP_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    servers = data.get("mcpServers") or {}
    if not servers:
        return None
    return str(SHARED_MCP_FILE)


def shared_mcp_config_path_for_role(role: str) -> str | None:
    """Role-aware MCP config path. Returns the per-role variant if the
    role has a policy entry (from pane-tools.json or built-in) and its
    variant exists; otherwise falls back to the master shared-mcp.json.

    Why: lets the orchestrator send each claude pane only the MCPs that
    role actually uses, cutting browser-MCP schemas (~12-16k tokens) out
    of panes that never call them.
    """
    # Check if role has an override or built-in policy. None = no policy
    # anywhere → master passthrough. An EMPTY set is a real policy ("this
    # role gets no MCPs") and must go through the variant path so the empty
    # variant returns None (skip --mcp-config) — `if allowed:` would flip
    # that into the full master config.
    allowed = effective_mcps(role, _ROLE_MCP_POLICY.get(role))
    if allowed is not None:  # role has policy (override or built-in)
        variant = _role_variant_path(role)
        if variant.is_file():
            try:
                data = json.loads(variant.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return shared_mcp_config_path()  # fall back on corruption
            servers = data.get("mcpServers") or {}
            if servers:
                return str(variant)
            # Empty allowlist intersection → no MCPs for this role: signal
            # "skip --mcp-config" by returning None.
            return None
    return shared_mcp_config_path()


# Each browser MCP's canonical "user data dir" CLI flag, taken from its own
# --help: @playwright/mcp uses kebab `--user-data-dir`; chrome-devtools-mcp
# documents camelCase `--userDataDir`. (yargs would also accept the kebab alias
# for chrome-devtools, but we hand each tool its documented form so the profile
# override never depends on camel-case expansion being enabled.)
_PROFILE_FLAG: dict[str, str] = {
    "playwright": "--user-data-dir",
    "chrome-devtools": "--userDataDir",
}

# Chromium "singleton" guard files live at the root of a user-data-dir and are
# what raise "profile is already in use / locked". A hard-killed shard (cockpit
# force-restart, watchdog os._exit, ConPTY freeze kill) leaves them behind; on
# Windows they don't self-recover, so a stale set would wedge the SAME shard's
# next run — re-introducing #39 one layer down. We best-effort clear them when
# (re)generating a shard config; the pane isn't alive yet, so no live browser
# owns the profile.
_SINGLETON_LOCK_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def _clear_stale_singleton_locks(profile_dir: pathlib.Path) -> None:
    for fname in _SINGLETON_LOCK_FILES:
        try:
            (profile_dir / fname).unlink(missing_ok=True)
        except OSError:
            pass  # a leftover lock is recoverable; crashing here is not


def browser_profile_mcp_config_path(
    base_role: str, shard_idx: int | None, project: str
) -> str | None:
    """Browser-profile-isolated MCP config: identical to the role variant, but
    each browser MCP (playwright, chrome-devtools) gets a PERSISTENT per-pane
    user-data-dir — via that browser's own profile flag (``--user-data-dir`` for
    playwright, ``--userDataDir`` for chrome-devtools). Two wins:

      * the browser **remembers its session/cookies across runs** instead of
        starting from playwright's default ephemeral temp profile — so a logged-in
        QA pane stays logged in next time;
      * parallel fan-out shards (``assign --shards N``) don't collide on one Chrome
        profile lock (#39 — only shard #1 could drive the browser, the rest hit
        "profile locked by another shard").

    The dir is keyed per (project, base_role[, shard], browser). ``shard_idx`` is
    None for a normal (non-fan-out) pane and an int for a shard, which is the only
    difference between the two callers — both get a persistent isolated profile.

    Non-browser MCPs pass through untouched. Returns the path to a generated
    ``shared-mcp-<project>-<role>[-shard<N>].json``; falls back to the plain
    role-variant path when the role has no browser MCP (nothing to isolate) or on
    any read/write error (a shared profile still beats no MCPs at all).
    """
    base_path = shared_mcp_config_path_for_role(base_role)
    if base_path is None:
        return None
    try:
        data = json.loads(pathlib.Path(base_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return base_path
    servers = data.get("mcpServers") or {}
    browser_names = [n for n in servers if n in _BROWSER_MCP_NAMES]
    if not browser_names:
        return base_path  # no browser MCP for this role — nothing to isolate

    # Sanitize the project namespace for use in file/dir names.
    safe_project = re.sub(r"[^A-Za-z0-9._-]", "_", project) or "default"
    shard_suffix = f"-shard{shard_idx}" if shard_idx is not None else ""
    profiles_root = SHARED_MCP_FILE.parent / "browser-profiles"
    for name in browser_names:
        flag = _PROFILE_FLAG.get(name, "--user-data-dir")
        cfg = dict(servers[name])
        args = list(cfg.get("args") or [])
        if flag in args:
            continue  # idempotent — already templated
        # Per (project, role[, shard], browser) profile dir — distinct browsers get
        # distinct dirs too (playwright Chromium vs chrome-devtools Chrome would
        # otherwise lock each other).
        profile_dir = profiles_root / f"{safe_project}-{base_role}{shard_suffix}-{name}"
        try:
            profile_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # Don't go silent — a path-too-long (Windows MAX_PATH) or permission
            # failure is the only signal that the profile won't isolate.
            _log.warning("browser_profile_mcp_config_path: could not create %s: %s", profile_dir, e)
        _clear_stale_singleton_locks(profile_dir)
        cfg["args"] = [*args, flag, str(profile_dir)]
        servers[name] = cfg
    data["mcpServers"] = servers

    out = SHARED_MCP_FILE.parent / f"shared-mcp-{safe_project}-{base_role}{shard_suffix}.json"
    try:
        out.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return base_path
    return str(out)


# Per-(project, role[, shard], browser) Chromium profile dirs created above are
# persistent on purpose: a QA pane stays logged in across runs (#39, 04ee5c6).
# But each new shard index / project / browser leaves a fresh dir behind forever,
# so runtime/browser-profiles/ grows unbounded (#42). We age-prune by mtime —
# Chromium bumps the dir mtime on every run, so mtime doubles as "last used", and
# a generous window keeps recently-used login profiles while reclaiming stale
# fan-out shards. Mirrors prune_old_transcripts() in orchestrator.py.
_BROWSER_PROFILE_RETENTION_DAYS = 14


def prune_old_browser_profiles(max_age_days: int = _BROWSER_PROFILE_RETENTION_DAYS) -> int:
    """Delete per-(project, role[, shard], browser) Chromium profile dirs under
    runtime/browser-profiles/ not used (by mtime) within *max_age_days*. Reclaims
    disk from #39 fan-out shards that otherwise accumulate forever (#42).

    Best-effort: never raises, returns the number of dirs removed. Call ONLY when
    no pane is live (e.g. at cockpit boot) — at startup no browser holds a profile
    open AND a recently-used login profile has a fresh mtime, so the age window
    keeps it. Do NOT call on pane/shard close: Windows holds the dir open while
    Chromium shuts down, and it would wipe the persistent login profile every run.
    """
    import shutil as _shutil
    import time as _time

    root = SHARED_MCP_FILE.parent / "browser-profiles"
    if not root.is_dir():
        return 0
    cutoff = _time.time() - max_age_days * 86_400
    removed = 0
    try:
        for p in root.iterdir():
            if not p.is_dir():
                continue  # leave stray files alone
            try:
                if p.stat().st_mtime < cutoff:
                    _shutil.rmtree(p, ignore_errors=True)
                    if not p.exists():  # don't count a partial delete (locked file mid-tree)
                        removed += 1
            except OSError:
                continue  # locked / MAX_PATH dir — skip, never crash startup
    except OSError:
        pass
    if removed:
        _log.info(
            "prune_old_browser_profiles: removed %d stale profile dir(s) (>%dd)",
            removed,
            max_age_days,
        )
    return removed


def ensure_browser_mcps() -> tuple[bool, str]:
    """Merge BROWSER_MCPS into runtime/shared-mcp.json if they're not
    already present. Idempotent — safe to call on every cockpit launch.

    Two startup states this has to handle without losing data:
      1. File missing — write a fresh file containing only the browser MCPs.
      2. File exists with browsers already — no-op (still returns ok).

    Returns (ok, message) for logging only; failures are non-fatal —
    panes still spawn, browser MCPs just won't be available until the
    file is healed by hand.
    """
    config: dict = {}
    if SHARED_MCP_FILE.is_file():
        try:
            config = json.loads(SHARED_MCP_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Corrupt file — refuse to clobber it. The user almost
            # certainly hand-edited and broke the JSON. Surface the
            # failure but leave the file alone.
            return False, f"could not parse {SHARED_MCP_FILE}; leaving as-is"
    servers = config.setdefault("mcpServers", {})
    changed = []
    for name, server_cfg in BROWSER_MCPS.items():
        desired = json.loads(json.dumps(server_cfg))  # deep copy
        if servers.get(name) != desired:
            servers[name] = desired
            changed.append(name)
    if not changed:
        # Ensure variants exist on first boot after upgrade (master may
        # be up-to-date but variants haven't been generated yet).
        _write_role_variants()
        return True, "browser MCPs already present"
    try:
        SHARED_MCP_FILE.parent.mkdir(parents=True, exist_ok=True)
        SHARED_MCP_FILE.write_text(
            json.dumps(config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as e:
        return False, f"could not write {SHARED_MCP_FILE}: {e}"
    _write_role_variants()
    return True, f"updated browser MCPs: {', '.join(changed)}"


# Browser MCP names are force-injected by ensure_browser_mcps() with pinned
# versions and specific flags.  User copies of the same names are skipped so
# cockpit's version always wins.
_BROWSER_MCP_NAMES = frozenset(BROWSER_MCPS.keys())

# Explicit allowlist of user MCP names that are safe to copy into
# runtime/shared-mcp.json by default.  Criteria: stdio servers with no
# bearer token, API-key, or inline DSN credentials.  Any name NOT in this
# set is evaluated by _has_secrets(); if that check fails the entry is
# skipped with a warning.
#
# Credential-bearing entries (bearer/API-key headers, or a DSN with inline
# `user:pass@host` credentials in args) are always skipped by _has_secrets()
# so a secret never lands in the world-readable shared runtime file.
# Emptied 2026-07-02: obsidian-vault's provider (claude-obsidian plugin) was
# uninstalled after the usage audit; no user MCP is trusted by default now.
# `takkub mcp add` / the Tools dialog are the supported install paths.
_USER_MCP_DEFAULT_ALLOW: frozenset[str] = frozenset()

# Role-aware MCP policy: which MCPs each role pane sees.
#
# Why: claude loads every tool schema from --mcp-config into the session
# context at spawn time. playwright + chrome-devtools have huge schemas
# (~24 + ~28 tools, each with full JSON parameter descriptions) that add
# 12-16k tokens to every pane regardless of whether the tools are used.
# Lead and most teammates never call browser MCPs directly — they're only
# meaningful for visual/UI work (qa smoke, critic shots, designer audit).
#
# Solution: per-role allowlist filters the master shared-mcp.json into a
# role-specific variant. Roles in this dict get only their allowed MCPs;
# roles NOT in this dict fall back to the full master file (back-compat
# for any future role we haven't classified yet).
#
# Policy rationale:
#   - lead: orchestrator only — delegates UI work, no direct browser use.
#   - qa: smoke + e2e tests need playwright/chrome-devtools.
#   - critic/designer: visual review reads shots, may inspect runtime DOM.
#   - reviewer/frontend/backend/mobile/devops: code roles work through the
#     dev server / shell / psql directly, not MCPs.
#   - codex/gemini: not claude — bypass --mcp-config entirely, listed here
#     for documentation only (won't be used; argv builder skips for them).
# obsidian-vault removed from every role 2026-07-02: the claude-obsidian
# plugin (its only provider) was uninstalled after a usage audit found 68
# calls across ~3,200 sessions. Non-browser roles keep an explicit EMPTY
# policy so they skip --mcp-config entirely (no schema tokens) instead of
# falling through to the master file.
_ROLE_MCP_POLICY: dict[str, frozenset[str]] = {
    "lead": frozenset(),
    "qa": frozenset({"playwright", "chrome-devtools"}),
    "critic": frozenset({"playwright", "chrome-devtools"}),
    "designer": frozenset({"playwright", "chrome-devtools"}),
    "reviewer": frozenset(),
    "frontend": frozenset(),
    "backend": frozenset(),
    "mobile": frozenset(),
    "devops": frozenset(),
}


def _role_variant_path(role: str) -> pathlib.Path:
    """Path to the per-role MCP config variant (filtered from master).
    Derived from SHARED_MCP_FILE so test fixtures that redirect that
    constant pick up the variants automatically."""
    return SHARED_MCP_FILE.parent / f"shared-mcp-{role}.json"


def add_mcp_server(name: str, cfg: dict, force: bool = False) -> bool:
    """Add or update an MCP server in the master shared-mcp.json.

    name: MCP name (validated against pattern [a-z0-9][a-z0-9_-]*)
    cfg: MCP config dict (type, command, args, env, etc.)
    force: if False and cfg has secrets, log warning and skip; if True,
           write despite secrets (for user opt-ins).

    Returns True on success, False on validation/I/O error.
    Blocks BROWSER_MCPS names from being overwritten (always returns False).
    Never raises.
    """
    from .pane_tools_policy import _validate_name

    if not isinstance(name, str) or not _validate_name(name):
        _log.warning("add_mcp_server: invalid MCP name %r", name)
        return False
    if name in _BROWSER_MCP_NAMES:
        _log.warning("add_mcp_server: cannot override browser MCP %r", name)
        return False
    if not isinstance(cfg, dict):
        _log.warning("add_mcp_server: cfg for %r is not dict", name)
        return False
    if not force and _has_secrets(cfg):
        _log.warning("add_mcp_server: skipping %r — credential-bearing entry", name)
        return False

    try:
        config: dict = {}
        if SHARED_MCP_FILE.is_file():
            config = json.loads(SHARED_MCP_FILE.read_text(encoding="utf-8"))
        servers = config.setdefault("mcpServers", {})
        servers[name] = cfg
        SHARED_MCP_FILE.parent.mkdir(parents=True, exist_ok=True)
        SHARED_MCP_FILE.write_text(
            json.dumps(config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        _write_role_variants()
        return True
    except OSError as e:
        _log.warning("add_mcp_server: could not write %s: %s", SHARED_MCP_FILE, e)
        return False


def remove_mcp_server(name: str) -> bool:
    """Remove an MCP server from the master shared-mcp.json.

    Blocks removal of browser MCPs (returns False without modifying file).
    Returns True on success, False on I/O error or if name not found.
    Never raises.
    """
    if name in _BROWSER_MCP_NAMES:
        _log.warning("remove_mcp_server: cannot remove browser MCP %r", name)
        return False

    try:
        if not SHARED_MCP_FILE.is_file():
            return False
        config = json.loads(SHARED_MCP_FILE.read_text(encoding="utf-8"))
        servers = config.get("mcpServers") or {}
        if name not in servers:
            return False
        del servers[name]
        SHARED_MCP_FILE.write_text(
            json.dumps(config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        _write_role_variants()
        return True
    except OSError as e:
        _log.warning("remove_mcp_server: could not update %s: %s", SHARED_MCP_FILE, e)
        return False


def list_master_mcps() -> dict[str, dict]:
    """Return all MCP servers from the master shared-mcp.json.

    Returns {} if file is missing or corrupt. Never raises.
    """
    if not SHARED_MCP_FILE.is_file():
        return {}
    try:
        config = json.loads(SHARED_MCP_FILE.read_text(encoding="utf-8"))
        return config.get("mcpServers") or {}
    except (OSError, json.JSONDecodeError):
        return {}


def regen_role_variants() -> int:
    """Regenerate all per-role MCP variant files from master.

    Returns count of role variants written. Non-fatal on error (logging
    only); never raises.
    """
    _write_role_variants()
    # Count files that exist and have servers.
    from .pane_tools_policy import load_policy

    count = 0
    for role in set(_ROLE_MCP_POLICY) | set(load_policy()):
        variant = _role_variant_path(role)
        if variant.is_file():
            try:
                data = json.loads(variant.read_text(encoding="utf-8"))
                if data.get("mcpServers"):
                    count += 1
            except (OSError, json.JSONDecodeError):
                pass
    return count


def _write_role_variants() -> None:
    """Regenerate every per-role MCP variant file from the master
    shared-mcp.json. Called after ensure_browser_mcps/ensure_user_mcps
    mutates the master so variants stay in sync.

    Failure is non-fatal: a missing variant simply causes the orchestrator
    to fall back to the master file for that role (back-compat).
    """
    if not SHARED_MCP_FILE.is_file():
        return
    try:
        master = json.loads(SHARED_MCP_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    master_servers: dict = master.get("mcpServers") or {}
    # Union of built-in roles and file-override roles: a role granted MCPs
    # only via pane-tools.json still needs its variant generated, otherwise
    # the UI/CLI edit silently never reaches a pane.
    from .pane_tools_policy import load_policy

    roles = set(_ROLE_MCP_POLICY) | set(load_policy())
    for role in sorted(roles):
        allowed = effective_mcps(role, _ROLE_MCP_POLICY.get(role))
        if allowed is None:
            continue  # no policy anywhere → master passthrough, no variant
        filtered = {name: cfg for name, cfg in master_servers.items() if name in allowed}
        # An empty allowlist intentionally writes an EMPTY variant — that is
        # what makes shared_mcp_config_path_for_role return None (skip
        # --mcp-config) for the role.
        variant = {"mcpServers": filtered}
        try:
            _role_variant_path(role).write_text(
                json.dumps(variant, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as e:
            _log.warning("_write_role_variants: could not write %s: %s", role, e)


# Patterns that indicate a credential-bearing MCP entry.  Any entry that
# matches is skipped unless it is explicitly in _USER_MCP_DEFAULT_ALLOW.
_SECRET_HEADER_KEYS = frozenset({"Authorization", "authorization"})
_SECRET_ENV_SUBSTRINGS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASS")


def _has_secrets(cfg: dict) -> bool:
    """Return True if *cfg* contains a credential that should not be written
    to a world-accessible shared runtime file."""
    headers = cfg.get("headers") or {}
    for key in _SECRET_HEADER_KEYS:
        if key in headers:
            return True
    env = cfg.get("env") or {}
    for var_name in env:
        upper = str(var_name).upper()
        if any(s in upper for s in _SECRET_ENV_SUBSTRINGS):
            return True
    args = cfg.get("args") or []
    for a in args:
        # DSN with inline credentials: scheme://user:pass@host
        if re.search(r"://[^/@\s]+:[^/@\s]+@", str(a)):
            return True
    return False


def mask_secrets(cfg: dict) -> dict:
    """Return a copy of *cfg* with credential-bearing values replaced by a
    masked placeholder — same patterns `_has_secrets` detects, so a UI can
    show that a server carries a credential without ever displaying it
    (SPEC.md "MCP Servers" credential handling). Never raises."""
    out = dict(cfg)
    headers = out.get("headers")
    if isinstance(headers, dict):
        out["headers"] = {
            k: ("••••••••" if k in _SECRET_HEADER_KEYS else v) for k, v in headers.items()
        }
    env = out.get("env")
    if isinstance(env, dict):
        out["env"] = {
            k: ("••••••••" if any(s in str(k).upper() for s in _SECRET_ENV_SUBSTRINGS) else v)
            for k, v in env.items()
        }
    args = out.get("args")
    if isinstance(args, list):
        out["args"] = [re.sub(r"(://[^/@\s]+):[^/@\s]+@", r"\1:••••••••@", str(a)) for a in args]
    return out


def default_role_mcp_policy() -> dict[str, frozenset[str]]:
    """Public read accessor for the built-in per-role MCP visibility table
    (`_ROLE_MCP_POLICY`), used by `settings_management`'s MCP repository to
    compute which roles see a given MCP absent any `pane_tools_policy`
    override."""
    return dict(_ROLE_MCP_POLICY)


def ensure_user_mcps() -> tuple[bool, str]:
    """Merge allowlisted user MCPs from ~/.claude.json into shared-mcp.json.

    Called after ensure_browser_mcps() so browser MCPs are already present
    and take precedence on name collision.

    Policy:
    - Only names in _USER_MCP_DEFAULT_ALLOW are included by default.
    - Any entry not in the default allow set AND carrying a secret (bearer
      token, API key, etc.) is skipped with a warning.
    - Browser MCP names (playwright, chrome-devtools) are never overwritten;
      user copies are skipped and logged.
    - Authorization header values are never written to logs.
    - ~/.claude.json read failure → log warning, skip silently (non-fatal).
    - shared-mcp.json corrupt → refuse to touch it.

    Returns (ok, message) for logging only; failure is non-fatal.
    """
    home = pathlib.Path.home()
    claude_json = home / ".claude.json"

    # --- read user MCPs ---
    try:
        raw = claude_json.read_text(encoding="utf-8")
        user_data = json.loads(raw)
    except FileNotFoundError:
        return True, "~/.claude.json not found; skipping user MCP merge"
    except (OSError, json.JSONDecodeError) as e:
        _log.warning("ensure_user_mcps: could not read ~/.claude.json: %s", e)
        return True, f"skipped user MCP merge: {e}"

    # top-level mcpServers only (not per-project entries nested under `projects`)
    user_servers: dict = user_data.get("mcpServers") or {}
    if not user_servers:
        return True, "no mcpServers in ~/.claude.json; nothing to merge"

    # --- classify each entry ---
    to_merge: dict[str, dict] = {}
    skipped: list[str] = []

    for name, cfg in user_servers.items():
        if name in _BROWSER_MCP_NAMES:
            skipped.append(f"{name} (browser MCP wins)")
            continue
        if not isinstance(cfg, dict):
            skipped.append(f"{name} (not a dict)")
            continue

        in_allowlist = name in _USER_MCP_DEFAULT_ALLOW
        is_secret = _has_secrets(cfg)

        if in_allowlist and is_secret:
            _log.warning(
                "ensure_user_mcps: %r is allowlisted but carries a credential "
                "(written to runtime/shared-mcp.json). Consider rotating to a "
                "credential-free config or env-based secret.",
                name,
            )

        if not in_allowlist and is_secret:
            _log.warning(
                "ensure_user_mcps: skipping %r — credential-bearing entry not in default allowlist",
                name,
            )
            skipped.append(f"{name} (skipped: credential-bearing)")
            continue

        to_merge[name] = cfg

    if not to_merge:
        return True, f"no eligible user MCPs to merge (skipped: {', '.join(skipped) or 'none'})"

    # --- read/update shared-mcp.json ---
    config: dict = {}
    if SHARED_MCP_FILE.is_file():
        try:
            config = json.loads(SHARED_MCP_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False, f"could not parse {SHARED_MCP_FILE}; leaving as-is"

    servers = config.setdefault("mcpServers", {})

    # Prune stale entries: non-browser user MCPs no longer in current policy.
    # Log name only — never the cfg value (may contain bearer tokens).
    pruned: list[str] = []
    for name in list(servers.keys()):
        if name in _BROWSER_MCP_NAMES:
            continue  # managed by ensure_browser_mcps; never touch
        if name not in to_merge:
            del servers[name]
            pruned.append(name)
            _log.info("ensure_user_mcps: pruned stale entry %r", name)

    changed: list[str] = []
    for name, cfg in to_merge.items():
        desired = json.loads(json.dumps(cfg))  # deep copy
        if servers.get(name) != desired:
            servers[name] = desired
            changed.append(name)

    if not changed and not pruned:
        # Even when master is unchanged, ensure variants exist (first boot
        # after upgrade: master may already be up-to-date but variants
        # haven't been generated yet).
        _write_role_variants()
        return True, "user MCPs already up-to-date in shared-mcp.json"

    try:
        SHARED_MCP_FILE.parent.mkdir(parents=True, exist_ok=True)
        SHARED_MCP_FILE.write_text(
            json.dumps(config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as e:
        return False, f"could not write {SHARED_MCP_FILE}: {e}"
    _write_role_variants()

    # log names only — never cfg values (may contain bearer tokens)
    parts: list[str] = []
    if changed:
        parts.append(f"merged: {', '.join(changed)}")
    if pruned:
        parts.append(f"pruned: {', '.join(pruned)}")
    return True, "; ".join(parts)
