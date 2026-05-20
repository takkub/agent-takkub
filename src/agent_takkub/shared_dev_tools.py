"""Cockpit-managed shared dev-tool config.

Some dev tools should follow Lead into every project tab without
requiring per-project `.claude/settings.json` edits:

  * **Pordee** — handled at the plugin layer via `_SAFE_PLUGINS` in
    orchestrator.py, no settings file involved.
  * **Browser MCPs** — playwright + chrome-devtools are injected into
    every pane via `runtime/shared-mcp.json` so smoke tests, UX checks,
    and crawls are available from any project's Lead without per-project
    wiring.
  * **rtk hook** — still per-project (the PreToolUse Bash hook lives
    in `.claude/settings.json`). Use the `⚡ Install rtk` button to add
    it to a specific project.

The shared-mcp.json file lives under `runtime/` (gitignored). The
cockpit writes it at startup via `ensure_browser_mcps()` and every
claude spawn receives it via `--mcp-config`.
"""

from __future__ import annotations

import json
import subprocess
import threading

from ._win_console import SUBPROCESS_NO_WINDOW
from .config import RUNTIME_DIR

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
        return True, "browser MCPs already present"
    try:
        SHARED_MCP_FILE.parent.mkdir(parents=True, exist_ok=True)
        SHARED_MCP_FILE.write_text(
            json.dumps(config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as e:
        return False, f"could not write {SHARED_MCP_FILE}: {e}"
    return True, f"updated browser MCPs: {', '.join(changed)}"
