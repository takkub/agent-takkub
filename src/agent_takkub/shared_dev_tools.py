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
from pathlib import Path

from .config import RUNTIME_DIR


SHARED_MCP_FILE = RUNTIME_DIR / "shared-mcp.json"

# The pms MCP server config. The bearer token is supplied by the user
# at setup time (initially via the cockpit's "Setup pms MCP" flow) and
# stored in shared-mcp.json next to this module. We embed an empty
# placeholder in the template so a missing token is obvious.
_PMS_MCP_TEMPLATE = {
    "mcpServers": {
        "pms": {
            "url": "https://api.wsol.co.th/pms/mcp",
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


def write_shared_mcp_config(token: str) -> tuple[bool, str]:
    """Persist the pms MCP server config with the supplied bearer token.

    `token` is the raw token (without the `Bearer ` prefix); the prefix is
    added here so the user can paste either form and we still produce
    valid input. Returns (ok, message).
    """
    token = (token or "").strip()
    if not token:
        return False, "token is empty"
    if token.lower().startswith("bearer "):
        token = token.split(None, 1)[1]
    config = json.loads(json.dumps(_PMS_MCP_TEMPLATE))  # deep copy
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


def clear_shared_mcp_config() -> None:
    """Remove the shared MCP config file. The next claude spawn will
    not pass `--mcp-config` and pms tools will become unavailable."""
    try:
        if SHARED_MCP_FILE.exists():
            SHARED_MCP_FILE.unlink()
    except OSError:
        pass


def shared_mcp_config_path() -> str | None:
    """Absolute path to the shared MCP config file if it exists and is
    usable. Returned to the orchestrator's argv builder."""
    if shared_mcp_config_exists():
        return str(SHARED_MCP_FILE)
    return None
