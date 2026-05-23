"""Cockpit-managed shared dev-tool config.

Some dev tools should follow Lead into every project tab without
requiring per-project `.claude/settings.json` edits:

  * **Pordee** — handled at the plugin layer via `_SAFE_PLUGINS` in
    orchestrator.py, no settings file involved.
  * **Browser MCPs** — playwright + chrome-devtools are injected into
    every pane via `runtime/shared-mcp.json` so smoke tests, UX checks,
    and crawls are available from any project's Lead without per-project
    wiring.
  * **User MCPs** — user's own `~/.claude.json` mcpServers (obsidian-vault,
    pms, postgres-pms, etc.) are merged into `runtime/shared-mcp.json` so
    every cockpit pane inherits them automatically without manual setup.
    Browser MCPs (playwright, chrome-devtools) take precedence on name
    collision. Entries without a `type` field are skipped.
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
import pathlib
import subprocess
import threading

from ._win_console import SUBPROCESS_NO_WINDOW
from .config import RUNTIME_DIR

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
    """

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
    role has a policy entry and its variant exists; otherwise falls back
    to the master shared-mcp.json (full schema).

    Why: lets the orchestrator send each claude pane only the MCPs that
    role actually uses, cutting browser-MCP schemas (~12-16k tokens) out
    of panes that never call them.
    """
    if role in _ROLE_MCP_POLICY:
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
# bearer token or API-key credentials.  Any name NOT in this set is
# evaluated by _is_secret_bearing(); if that check fails the entry is
# skipped with a warning.
#
# To include pms (HTTP + Authorization header) cockpit-wide, set env:
#   TAKKUB_INCLUDE_PMS=1
# This is opt-in because pms config carries a plaintext bearer token and
# merging it into shared-mcp.json re-introduces the security regression
# that was explicitly removed in the 2026-05-20 security audit.
_USER_MCP_DEFAULT_ALLOW = frozenset({"obsidian-vault", "postgres-pms"})

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
#   - reviewer: code review may query DB for context; no browser.
#   - frontend/mobile: UI dev but uses dev server + DOM directly, not MCPs.
#   - backend/devops: may query DB; no browser.
#   - codex/gemini: not claude — bypass --mcp-config entirely, listed here
#     for documentation only (won't be used; argv builder skips for them).
_ROLE_MCP_POLICY: dict[str, frozenset[str]] = {
    "lead": frozenset({"obsidian-vault", "postgres-pms"}),
    "qa": frozenset({"playwright", "chrome-devtools", "obsidian-vault", "postgres-pms"}),
    "critic": frozenset({"playwright", "chrome-devtools", "obsidian-vault"}),
    "designer": frozenset({"playwright", "chrome-devtools", "obsidian-vault"}),
    "reviewer": frozenset({"obsidian-vault", "postgres-pms"}),
    "frontend": frozenset({"obsidian-vault"}),
    "backend": frozenset({"obsidian-vault", "postgres-pms"}),
    "mobile": frozenset({"obsidian-vault"}),
    "devops": frozenset({"obsidian-vault", "postgres-pms"}),
}


def _role_variant_path(role: str) -> pathlib.Path:
    """Path to the per-role MCP config variant (filtered from master).
    Derived from SHARED_MCP_FILE so test fixtures that redirect that
    constant pick up the variants automatically."""
    return SHARED_MCP_FILE.parent / f"shared-mcp-{role}.json"


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
    for role, allowed in _ROLE_MCP_POLICY.items():
        filtered = {name: cfg for name, cfg in master_servers.items() if name in allowed}
        variant = {"mcpServers": filtered}
        try:
            _role_variant_path(role).write_text(
                json.dumps(variant, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as e:
            _log.warning("_write_role_variants: could not write %s: %s", role, e)


# Patterns that indicate a credential-bearing MCP entry.  Any entry that
# matches is skipped unless the user has opted in via TAKKUB_INCLUDE_PMS
# (for pms specifically) or is explicitly in _USER_MCP_DEFAULT_ALLOW.
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
    return False


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
    - pms is skipped by default (HTTP + bearer token); set TAKKUB_INCLUDE_PMS=1
      to include it despite the credential risk.
    - Authorization header values are never written to logs.
    - ~/.claude.json read failure → log warning, skip silently (non-fatal).
    - shared-mcp.json corrupt → refuse to touch it.

    Returns (ok, message) for logging only; failure is non-fatal.
    """
    import os

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

    include_pms = os.environ.get("TAKKUB_INCLUDE_PMS", "").strip() == "1"

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

        # pms-specific opt-in gate — evaluate before the general secret check
        # so that TAKKUB_INCLUDE_PMS=1 actually reaches to_merge.
        if name == "pms":
            if not include_pms:
                skipped.append(
                    f"{name} (skipped: HTTP+bearer; set TAKKUB_INCLUDE_PMS=1 to include)"
                )
                continue
            # User explicitly opted in — include despite bearer token.
            to_merge[name] = cfg
            continue

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
