"""Role-aware pane tools (MCPs + plugins) policy system.

Configurable via ~/.takkub/pane-tools.json: per-role allowlists for MCPs
and plugins. Roles not in the policy fall back to defaults. Enables cockpit
operator to control which tools each teammate sees without code edits.

Schema:
  {
    "version": 1,
    "roles": {
      "<role>": {
        "mcps": ["name", ...],
        "plugins": ["name", ...]
      }
    }
  }

Missing roles/keys default to built-in policy. Validation: role names must
match [a-z0-9][a-z0-9_-]*. Load/save are atomic (tmp+replace) and safe
against concurrent access.
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import tempfile

from .config import SETTINGS_HOME

_log = logging.getLogger(__name__)

# All known roles in cockpit (controls which keys are valid in policy file).
KNOWN_ROLES = frozenset(
    {
        "lead",
        "frontend",
        "backend",
        "mobile",
        "devops",
        "qa",
        "reviewer",
        "critic",
        "designer",
        "codex",
        "gemini",
        "analyst",
        "security",
        "docs",
    }
)

PANE_TOOLS_POLICY_FILE = SETTINGS_HOME / "pane-tools.json"


def _policy_dir() -> pathlib.Path:
    """Ensure ~/.takkub/ directory exists; idempotent."""
    d = PANE_TOOLS_POLICY_FILE.parent
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def _validate_name(name: str) -> bool:
    """Check if name matches [a-z0-9][a-z0-9_-]*."""
    return bool(re.match(r"^[a-z0-9][a-z0-9_-]*$", name, re.IGNORECASE))


def load_policy() -> dict[str, dict[str, list[str]]]:
    """Load role-specific MCP and plugin overrides from pane-tools.json.

    Returns { role: { "mcps": [...], "plugins": [...] }, ... }

    If file is missing, corrupt, or empty → return {}. Never raises.
    Safe to call at any time; locks not needed (single JSON reader).
    """
    if not PANE_TOOLS_POLICY_FILE.is_file():
        return {}
    try:
        data = json.loads(PANE_TOOLS_POLICY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _log.debug("load_policy: could not read %s: %s", PANE_TOOLS_POLICY_FILE, e)
        return {}

    if not isinstance(data, dict):
        _log.warning("load_policy: file root is not dict, treating as empty")
        return {}

    roles = data.get("roles")
    if not isinstance(roles, dict):
        _log.debug("load_policy: 'roles' key missing or not dict")
        return {}

    # Filter to known roles only; log if unrecognized role in file.
    out: dict[str, dict[str, list[str]]] = {}
    for role, entry in roles.items():
        if not isinstance(role, str) or role not in KNOWN_ROLES:
            if isinstance(role, str):
                _log.debug("load_policy: skipping unknown role %r", role)
            continue
        if not isinstance(entry, dict):
            _log.debug("load_policy: role %r value is not dict, skipping", role)
            continue

        mcps = entry.get("mcps")
        plugins = entry.get("plugins")
        if not isinstance(mcps, list):
            mcps = []
        if not isinstance(plugins, list):
            plugins = []

        # Validate individual names.
        valid_mcps = [n for n in mcps if isinstance(n, str) and _validate_name(n)]
        valid_plugins = [n for n in plugins if isinstance(n, str) and _validate_name(n)]
        if len(valid_mcps) != len(mcps) or len(valid_plugins) != len(plugins):
            _log.debug("load_policy: role %r has invalid name(s), filtering", role)

        out[role] = {
            "mcps": valid_mcps,
            "plugins": valid_plugins,
        }

    return out


def save_policy(policy: dict[str, dict[str, list[str]]]) -> bool:
    """Atomically write policy to ~/.takkub/pane-tools.json.

    policy: { role: { "mcps": [...], "plugins": [...] }, ... }

    Validates all role names and item names before writing. Returns True
    on success, False on validation error or I/O failure. Never raises.
    If policy is empty, deletes the file and returns True (idempotent).
    """
    # Empty policy is ok; delete file and return success.
    if not policy:
        try:
            PANE_TOOLS_POLICY_FILE.unlink(missing_ok=True)
        except OSError as e:
            _log.warning("save_policy: could not delete %s: %s", PANE_TOOLS_POLICY_FILE, e)
            return False
        return True

    # Validate input.
    for role, entry in policy.items():
        if not isinstance(role, str) or role not in KNOWN_ROLES:
            _log.warning("save_policy: rejecting invalid role %r", role)
            return False
        if not isinstance(entry, dict):
            _log.warning("save_policy: role %r value is not dict", role)
            return False

        for kind in ("mcps", "plugins"):
            items = entry.get(kind)
            if items is None:
                _log.warning("save_policy: role %r missing %r key", role, kind)
                return False
            if not isinstance(items, list):
                _log.warning("save_policy: role %r %r is not list", role, kind)
                return False
            for item in items:
                if not isinstance(item, str) or not _validate_name(item):
                    _log.warning("save_policy: role %r %r has invalid name %r", role, kind, item)
                    return False

    # Atomic write via tmp + replace.
    payload = {
        "version": 1,
        "roles": policy,
    }
    try:
        _policy_dir()
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=PANE_TOOLS_POLICY_FILE.parent,
            suffix=".json",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            json.dump(payload, tmp, indent=2, ensure_ascii=False)
            tmp.write("\n")
            tmp_path = pathlib.Path(tmp.name)
        tmp_path.replace(PANE_TOOLS_POLICY_FILE)
        return True
    except OSError as e:
        _log.warning("save_policy: could not write %s: %s", PANE_TOOLS_POLICY_FILE, e)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def effective_mcps(role: str, default: frozenset[str] | None = None) -> frozenset[str] | None:
    """Get effective MCP allowlist for role: file override or default.

    Reads pane-tools.json once per call. If role has an override in the
    policy file, returns a frozenset of MCP names — possibly EMPTY, which
    means "this role gets no MCPs" and must stay distinguishable from
    "no policy". Otherwise returns `default` verbatim, so a None default
    propagates as None = "no policy anywhere" (callers fall back to the
    master passthrough). Collapsing both cases to frozenset() inverted
    the semantics: an empty allowlist read as falsy and the role received
    the FULL master config instead of none.
    """
    policy = load_policy()
    if role in policy:
        mcps = policy[role].get("mcps") or []
        return frozenset(mcps)
    return default


def effective_plugins(role: str, default: frozenset[str] | None = None) -> frozenset[str] | None:
    """Get effective plugin allowlist for role: file override or default.

    Same None-vs-empty contract as `effective_mcps`: an override returns a
    (possibly empty) frozenset; no override returns `default` verbatim,
    including None.
    """
    policy = load_policy()
    if role in policy:
        plugins = policy[role].get("plugins") or []
        return frozenset(plugins)
    return default


def set_role_items(role: str, kind: str, names: list[str]) -> bool:
    """Update MCP or plugin allowlist for a role in the policy file.

    kind: "mcps" or "plugins"
    names: list of item names to allow for this role

    Returns True on success, False on validation/I/O error.
    Never raises.
    """
    if kind not in ("mcps", "plugins"):
        _log.warning("set_role_items: invalid kind %r", kind)
        return False
    if role not in KNOWN_ROLES:
        _log.warning("set_role_items: invalid role %r", role)
        return False
    if not all(isinstance(n, str) and _validate_name(n) for n in names):
        _log.warning("set_role_items: role %r %r has invalid name(s)", role, kind)
        return False

    policy = load_policy()
    if role not in policy:
        policy[role] = {"mcps": [], "plugins": []}
    policy[role][kind] = names
    return save_policy(policy)


def allow_item(role: str, kind: str, name: str) -> bool:
    """Add an MCP or plugin to a role's allowlist (idempotent).

    Returns True on success, False on validation/I/O error.
    """
    if kind not in ("mcps", "plugins") or role not in KNOWN_ROLES:
        return False
    if not _validate_name(name):
        _log.warning("allow_item: invalid name %r", name)
        return False

    policy = load_policy()
    if role not in policy:
        policy[role] = {"mcps": [], "plugins": []}

    items = list(policy[role].get(kind) or [])
    if name not in items:
        items.append(name)
    policy[role][kind] = items
    return save_policy(policy)


def deny_item(role: str, kind: str, name: str) -> bool:
    """Remove an MCP or plugin from a role's allowlist (idempotent).

    Returns True on success, False on validation/I/O error.
    """
    if kind not in ("mcps", "plugins") or role not in KNOWN_ROLES:
        return False
    if not _validate_name(name):
        _log.warning("deny_item: invalid name %r", name)
        return False

    policy = load_policy()
    if role not in policy:
        return True  # already not present

    items = list(policy[role].get(kind) or [])
    if name in items:
        items.remove(name)
    # Always preserve the role's other items
    policy[role][kind] = items
    # Preserve the complete policy when saving
    return save_policy(policy)


def reset_role(role: str) -> bool:
    """Remove all overrides for a role (reverts to built-in defaults).

    Returns True on success, False on I/O error.
    """
    if role not in KNOWN_ROLES:
        _log.warning("reset_role: invalid role %r", role)
        return False

    policy = load_policy()
    if role in policy:
        del policy[role]
    # save_policy handles empty policies gracefully (deletes file)
    return save_policy(policy)
