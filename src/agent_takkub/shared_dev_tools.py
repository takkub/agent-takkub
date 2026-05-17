"""Cockpit-managed shared dev-tool config.

Some dev tools should follow Lead into every project tab without
requiring per-project `.claude/settings.json` edits:

  * **Pordee** — handled at the plugin layer via `_SAFE_PLUGINS` in
    orchestrator.py, no settings file involved.
  * **pms MCP server** — exposed by pms-api at
    https://api.wsol.co.th/pms/mcp. The orchestrator passes this
    config to every claude spawn via `--mcp-config`, so the MCP tools
    are available regardless of which project's `.claude/settings.json`
    Lead happens to land on.
  * **rtk hook** — still per-project (the PreToolUse Bash hook lives
    in `.claude/settings.json` and `claude --settings <file>` doesn't
    merge `hooks` from an arbitrary file in this version). Use the
    `⚡ Install rtk` button to add it to a specific project.

The pms-mcp config is stored under `runtime/shared-mcp.json`. Because
the file contains a bearer token, `runtime/` is already in .gitignore
so it never reaches a public repo. The cockpit creates the file on
first launch (or when the user opts in via the UI) and the file is
read by every subsequent claude spawn.
"""

from __future__ import annotations

import json

from .config import RUNTIME_DIR


SHARED_MCP_FILE = RUNTIME_DIR / "shared-mcp.json"

# pms MCP endpoint. Pinned to production — the cockpit owner does not
# want a dev variant in the picker (any reference to "pms-dev" tends to
# leak into Lead's context and trigger searches for a `mcp__pms-dev__*`
# tool that doesn't exist).
PMS_MCP_URL_PROD = "https://api.wsol.co.th/pms/mcp"
PMS_MCP_DEFAULT_URL = PMS_MCP_URL_PROD

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
BROWSER_MCPS: dict = {
    "playwright": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@playwright/mcp@latest"],
        "env": {},
    },
    "chrome-devtools": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "chrome-devtools-mcp@latest"],
        "env": {},
    },
}

# The pms MCP server config. The bearer token is supplied by the user
# at setup time (initially via the cockpit's "Setup pms MCP" flow) and
# stored in shared-mcp.json next to this module. We embed an empty
# placeholder in the template so a missing token is obvious.
_PMS_MCP_TEMPLATE = {
    "mcpServers": {
        "pms": {
            # `type: "http"` is required for claude code to recognise this
            # as a streaming-HTTP MCP server. Without it claude can't pick
            # a transport, silently drops the entry, and `/mcp` reports
            # "No MCP servers configured" — symptom we hit in dogfooding.
            "type": "http",
            "url": PMS_MCP_DEFAULT_URL,
            "headers": {"Authorization": "Bearer <PMS_TOKEN_HERE>"},
        }
    },
    # Pre-allow every pms tool the server exposes so Lead can use them
    # without permission prompts. Includes both read and write paths
    # (`pms_create_task`, `pms_update_task`, `pms_add_comment`) so
    # Finish Job's task-creation flow doesn't stall on consent dialogs.
    "permissions": {
        "allow": [
            "mcp__pms__pms_preview_task",
            "mcp__pms__pms_get_task",
            "mcp__pms__pms_list_tasks",
            "mcp__pms__pms_list_workspaces",
            "mcp__pms__pms_list_spaces",
            "mcp__pms__pms_list_lists",
            "mcp__pms__pms_list_statuses",
            "mcp__pms__pms_resolve_list",
            "mcp__pms__pms_create_task",
            "mcp__pms__pms_update_task",
            "mcp__pms__pms_add_comment",
        ]
    },
}


def shared_mcp_config_exists() -> bool:
    """True when the cockpit has a usable shared MCP config file. We
    treat a token of `<PMS_TOKEN_HERE>` as "not yet configured" so the
    UI keeps nudging the user until a real bearer is in place."""
    if not SHARED_MCP_FILE.is_file():
        return False
    try:
        data = json.loads(SHARED_MCP_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    pms = (data.get("mcpServers") or {}).get("pms") or {}
    auth = (pms.get("headers") or {}).get("Authorization") or ""
    return "PMS_TOKEN_HERE" not in auth and auth.startswith("Bearer ")


def write_shared_mcp_config(
    token: str, url: str = PMS_MCP_DEFAULT_URL
) -> tuple[bool, str]:
    """Persist the pms MCP server config with the supplied bearer token
    and endpoint URL.

    `token` is the raw token (without the `Bearer ` prefix); the prefix is
    added here so the user can paste either form and we still produce
    valid input. `url` lets the caller flip between prod / dev / custom
    endpoints without hand-editing JSON. Returns (ok, message).
    """
    token = (token or "").strip()
    if not token:
        return False, "token is empty"
    if token.lower().startswith("bearer "):
        token = token.split(None, 1)[1]
    url = (url or PMS_MCP_DEFAULT_URL).strip()
    if not url.startswith(("http://", "https://")):
        return False, f"url must start with http(s)://, got: {url!r}"

    config = json.loads(json.dumps(_PMS_MCP_TEMPLATE))  # deep copy
    config["mcpServers"]["pms"]["url"] = url
    config["mcpServers"]["pms"]["headers"]["Authorization"] = f"Bearer {token}"
    try:
        SHARED_MCP_FILE.parent.mkdir(parents=True, exist_ok=True)
        SHARED_MCP_FILE.write_text(
            json.dumps(config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as e:
        return False, f"could not write {SHARED_MCP_FILE}: {e}"
    return True, f"shared MCP config written: {SHARED_MCP_FILE}"


def read_shared_mcp_config() -> tuple[str | None, str | None]:
    """Return (url, masked_token) for the current shared config, or
    (None, None) if it's not configured yet. The token is masked
    (`pms_***…last4`) so callers can display it safely in a UI without
    revealing the full bearer."""
    if not SHARED_MCP_FILE.is_file():
        return None, None
    try:
        data = json.loads(SHARED_MCP_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    pms = (data.get("mcpServers") or {}).get("pms") or {}
    url = pms.get("url")
    auth = (pms.get("headers") or {}).get("Authorization") or ""
    token = auth.split(None, 1)[1] if auth.lower().startswith("bearer ") else None
    if token and len(token) > 8:
        token = f"{token[:4]}***{token[-4:]}"
    return url, token


def clear_shared_mcp_config() -> None:
    """Remove the shared MCP config file. The next claude spawn will
    not pass `--mcp-config` and pms tools will become unavailable."""
    try:
        if SHARED_MCP_FILE.exists():
            SHARED_MCP_FILE.unlink()
    except OSError:
        pass


def shared_mcp_config_path() -> str | None:
    """Absolute path to the shared MCP config file if it exists and has
    at least one usable MCP entry (pms with a real bearer, or any of
    the browser MCPs). Returned to the orchestrator's argv builder."""
    if not SHARED_MCP_FILE.is_file():
        return None
    try:
        data = json.loads(SHARED_MCP_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    servers = data.get("mcpServers") or {}
    if not servers:
        return None
    # If pms is the only thing in there, it must have a real bearer.
    # Otherwise (browser MCPs alone, or browser + pms) the file is
    # always worth handing to claude.
    if set(servers.keys()) == {"pms"}:
        return str(SHARED_MCP_FILE) if shared_mcp_config_exists() else None
    return str(SHARED_MCP_FILE)


def ensure_browser_mcps() -> tuple[bool, str]:
    """Merge BROWSER_MCPS into runtime/shared-mcp.json if they're not
    already present. Idempotent — safe to call on every cockpit launch.

    Three startup states this has to handle without losing data:
      1. File missing — write a fresh file containing only the browser
         MCPs (no pms section).
      2. File exists with pms only — preserve the pms entry and the
         user's bearer token, just add the browser MCPs alongside.
      3. File exists with browsers already — no-op (still returns ok
         so callers don't need to special-case "already done").

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
    added = []
    for name, server_cfg in BROWSER_MCPS.items():
        if name not in servers:
            servers[name] = json.loads(json.dumps(server_cfg))  # deep copy
            added.append(name)
    if not added:
        return True, "browser MCPs already present"
    try:
        SHARED_MCP_FILE.parent.mkdir(parents=True, exist_ok=True)
        SHARED_MCP_FILE.write_text(
            json.dumps(config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as e:
        return False, f"could not write {SHARED_MCP_FILE}: {e}"
    return True, f"added browser MCPs: {', '.join(added)}"
