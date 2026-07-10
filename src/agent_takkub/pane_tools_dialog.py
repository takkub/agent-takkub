"""Pure per-role MCP/plugin policy helpers, shared by settings_window.py.

Originally backed a standalone "🔧 Tools" QDialog (role x MCP/plugin
checkbox matrix + a "Team & Roles" tab). That dialog was removed
2026-07-10 — 100% superseded by SettingsWindow's native "👥 Team" view
(VIEW_MCP_MATRIX / VIEW_PLUGINS_MATRIX / VIEW_SKILL_CATALOG / VIEW_NEW_ROLE),
which reads the SAME policy via the module-level functions below
(``build_matrix``/``matrix_to_role_items``/``diff_role_items``/
``master_mcps``/``policy_role_items``/``discover_marketplaces``/
``parse_install_form``). Kept as plain functions (no Qt) so both this
module's tests and settings_window's callers stay display-free.

**Import constraint:** this module MUST NOT import ``app`` or ``cli``.
"""

from __future__ import annotations

import json
import pathlib

# Roles that get a row in the matrix. Order matches the cockpit's
# role-declaration convention (lead first, then specialists).
ROLES: tuple[str, ...] = (
    "lead",
    "frontend",
    "backend",
    "mobile",
    "devops",
    "qa",
    "reviewer",
    "critic",
    "designer",
    "analyst",
    "security",
    "docs",
)


def _default_plugins_installed_file() -> pathlib.Path:
    """``<config.default_claude_config_dir()>/plugins/installed_plugins.json``
    — plain ``~/.claude`` for a dev checkout, the isolated per-instance profile
    for an installed build. Resolved fresh (not a module constant) so it tracks
    ``config.DATA_HOME`` / ``Path.home`` even when a test monkeypatches them
    after import."""
    from .config import default_claude_config_dir

    return default_claude_config_dir() / "plugins" / "installed_plugins.json"


def build_matrix(
    roles: tuple[str, ...],
    items: list[str],
    role_items: dict[str, list[str]],
) -> dict[str, dict[str, bool]]:
    """Build ``matrix[role][item] = bool`` from each role's current item list.

    ``role_items`` maps role -> list of item names currently enabled for
    that role (already resolved through defaults by the caller, e.g. via
    ``pane_tools_policy.effective_mcps``).
    """
    matrix: dict[str, dict[str, bool]] = {}
    for role in roles:
        enabled = set(role_items.get(role, ()))
        matrix[role] = {item: item in enabled for item in items}
    return matrix


def matrix_to_role_items(matrix: dict[str, dict[str, bool]]) -> dict[str, list[str]]:
    """Inverse of ``build_matrix``: checked items per role, name-sorted."""
    return {
        role: sorted(item for item, checked in items.items() if checked)
        for role, items in matrix.items()
    }


def diff_role_items(
    original: dict[str, list[str]],
    updated: dict[str, list[str]],
) -> dict[str, tuple[list[str], list[str]]]:
    """Per-role ``(added, removed)`` item names between two role->items maps.

    Roles with no change are omitted so the caller only touches what
    actually changed.
    """
    changes: dict[str, tuple[list[str], list[str]]] = {}
    for role in set(original) | set(updated):
        before = set(original.get(role, ()))
        after = set(updated.get(role, ()))
        added = sorted(after - before)
        removed = sorted(before - after)
        if added or removed:
            changes[role] = (added, removed)
    return changes


def discover_marketplace_plugins(
    installed_file: pathlib.Path | None = None,
) -> list[str]:
    """Plugin names (``name@marketplace``) known to this machine's Claude
    plugin install registry. Missing/unreadable file -> empty list."""
    if installed_file is None:
        installed_file = _default_plugins_installed_file()
    try:
        data = json.loads(installed_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    plugins = data.get("plugins") or {}
    if not isinstance(plugins, dict):
        return []
    return sorted(plugins.keys())


def master_mcps() -> list[str]:
    """Master MCP registry names (``shared_dev_tools.list_master_mcps()``),
    empty on any failure. Module-level so non-Qt callers (settings_window's
    native MCP Matrix view) can build a matrix without instantiating a dialog."""
    try:
        from . import shared_dev_tools

        return list(shared_dev_tools.list_master_mcps())
    except Exception:
        return []


def policy_role_items(roles: tuple[str, ...], kind: str) -> dict[str, list[str]]:
    """Current per-role item names for *kind* ('mcps'/'plugins'), resolved
    through ``pane_tools_policy`` defaults. Module-level so it's shared
    between settings_window's native matrix views instead of being
    duplicated."""
    try:
        from . import pane_tools_policy, shared_dev_tools
        from .lead_context import _ROLE_PLUGIN_POLICY, _TEAMMATE_PLUGINS

        defaults = getattr(shared_dev_tools, "_ROLE_MCP_POLICY", {})
        result: dict[str, list[str]] = {}
        for role in roles:
            if kind == "mcps":
                default = frozenset(defaults.get(role, ()))
                result[role] = list(pane_tools_policy.effective_mcps(role, default) or ())
            else:
                # Real built-in default, NOT [] — otherwise the matrix
                # renders every plugin unchecked and a naive Save writes
                # deny-all overrides for every role.
                default = _ROLE_PLUGIN_POLICY.get(role, _TEAMMATE_PLUGINS)
                result[role] = list(pane_tools_policy.effective_plugins(role, default) or ())
        return result
    except Exception:
        return {role: [] for role in roles}


def discover_marketplaces(
    installed_file: pathlib.Path | None = None,
) -> list[str]:
    """Marketplace names the pane plugin-policy can actually govern.

    The plugin policy is **marketplace-granular**: ``_default_plugin_dirs``
    filters by marketplace and loads every plugin dir under an allowed one, so a
    role's effective plugin set is a set of *marketplace* names
    (``superpowers-dev``, ``pordee``, ``claude-plugins-official``) — never
    ``name@marketplace``. The Plugins matrix must therefore offer **marketplace
    columns** so a checkbox's identity matches what the policy stores and reads.

    Using ``discover_marketplace_plugins`` (``name@marketplace``) as columns made
    every cell compare e.g. ``code-review@claude-plugins-official`` against a
    ``claude-plugins-official`` policy entry — never equal, so the whole grid
    rendered unchecked even when the plugins were enabled, and a Save then wrote
    an empty deny-all override for every role. That was the 2026-07-02 wipe.

    Returns the installed marketplaces intersected with ``_SAFE_PLUGINS`` (the
    only ones pane injection can load), sorted. Missing/unreadable file → [].
    """
    from .config import _SAFE_PLUGINS

    if installed_file is None:
        installed_file = _default_plugins_installed_file()
    try:
        data = json.loads(installed_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    plugins = data.get("plugins") or {}
    if not isinstance(plugins, dict):
        return []
    governable = set(_SAFE_PLUGINS)
    # Install-registry keys are ``name@marketplace``; the segment after ``@`` is
    # the marketplace. Keep only marketplaces the pane loader can inject.
    found = {
        key.partition("@")[2]
        for key in plugins
        if isinstance(key, str) and key.partition("@")[2] in governable
    }
    return sorted(found)


def parse_install_form(name: str, command: str, args_line: str) -> tuple[str, dict] | None:
    """Turn the "add MCP" form fields into ``(name, cfg)`` for
    ``shared_dev_tools.add_mcp_server``. Returns ``None`` if the form is
    incomplete (caller shows a validation message instead of calling out)."""
    name = name.strip()
    command = command.strip()
    if not name or not command:
        return None
    args = args_line.split()
    return name, {"command": command, "args": args}
