"""Per-project Claude user-profile store.

Claude Code reads its login credentials from the directory pointed to by
``CLAUDE_CONFIG_DIR`` (defaults to ``~/.claude``).  When a user has
multiple Claude accounts they can register each as a named profile and
assign one per project — the cockpit then injects ``CLAUDE_CONFIG_DIR``
into every pane spawned for that project so it automatically logs in as
the right account.

Config files:
- ``~/.takkub/user-profiles.json`` — registry: list of
  ``{name, config_dir}`` objects.  The ``"default"`` profile is implicit
  (always resolves to ``~/.claude``) and is never stored in the file.
- ``~/.takkub/projects/<slug>/user-profile.json`` — per-project
  selection: ``{name: "<profile_name>"}``.  Absent → ``"default"``.

Failure policy: every public function returns a safe value (never raises
on missing/corrupt data).  The user's panes must always be able to spawn.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

_BASE_DIR = Path.home() / ".takkub"
_REGISTRY_PATH = _BASE_DIR / "user-profiles.json"
_DEFAULT_CONFIG_DIR = Path.home() / ".claude"
DEFAULT_PROFILE = "default"

# Valid profile names: 1-64 chars, alphanumeric + hyphens + underscores,
# cannot be the reserved name "default".
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _project_slug(project: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", project) or "default"


def _project_profile_path(project: str) -> Path:
    return _BASE_DIR / "projects" / _project_slug(project) / "user-profile.json"


def _atomic_write(path: Path, data: object) -> None:
    """Write JSON to *path* atomically (tmp → rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2) + "\n"
    # Write to a sibling temp file then rename for atomicity.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(text)
        Path(tmp).replace(path)
    except Exception:
        try:
            Path(tmp).unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _load_registry() -> list[dict]:
    """Load stored profiles; return [] on missing/corrupt."""
    try:
        raw = _REGISTRY_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        config_dir = str(item.get("config_dir", "")).strip()
        if name and config_dir and name != DEFAULT_PROFILE:
            out.append({"name": name, "config_dir": config_dir})
    return out


def list_profiles() -> list[dict]:
    """Return all profiles: implicit default first, then registered ones.

    Each entry: ``{"name": str, "config_dir": str}``.
    """
    registered = _load_registry()
    default_entry = {"name": DEFAULT_PROFILE, "config_dir": str(_DEFAULT_CONFIG_DIR)}
    return [default_entry, *registered]


def add_profile(name: str, config_dir: str | Path) -> None:
    """Register a new profile.

    Raises ``ValueError`` if *name* is invalid or already taken.
    No-ops on I/O errors to keep callers fault-tolerant (caller should
    handle the ValueError for UX, but not OSError).
    """
    name = str(name).strip()
    if not _NAME_RE.match(name):
        raise ValueError(
            f"Invalid profile name {name!r}: use 1-64 chars, letters/digits/hyphens/underscores"
        )
    if name == DEFAULT_PROFILE:
        raise ValueError("'default' is a reserved profile name")
    config_dir_s = str(config_dir).strip()
    if not config_dir_s:
        raise ValueError("config_dir must not be empty")

    profiles = _load_registry()
    if any(p["name"] == name for p in profiles):
        raise ValueError(f"Profile {name!r} already exists")
    profiles.append({"name": name, "config_dir": config_dir_s})
    try:
        _atomic_write(_REGISTRY_PATH, profiles)
    except OSError:
        pass


def remove_profile(name: str) -> None:
    """Remove a registered profile by name.

    Silent if not found.  Raises ``ValueError`` for the reserved default.
    """
    name = str(name).strip()
    if name == DEFAULT_PROFILE:
        raise ValueError("Cannot remove the implicit 'default' profile")
    profiles = _load_registry()
    updated = [p for p in profiles if p["name"] != name]
    try:
        _atomic_write(_REGISTRY_PATH, updated)
    except OSError:
        pass


def profile_for(project: str) -> str:
    """Return the profile name selected for *project* (``"default"`` if unset)."""
    path = _project_profile_path(project)
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return DEFAULT_PROFILE
    if not isinstance(data, dict):
        return DEFAULT_PROFILE
    name = str(data.get("name", "")).strip()
    if not name:
        return DEFAULT_PROFILE
    # Verify the name still exists in the registry (profile may have been removed)
    if name != DEFAULT_PROFILE:
        registry = _load_registry()
        if not any(p["name"] == name for p in registry):
            return DEFAULT_PROFILE
    return name


def set_profile(project: str, name: str) -> None:
    """Assign a profile to *project*.

    Raises ``ValueError`` if *name* is not in the registry (and not
    ``"default"``).  Silent on I/O errors.
    """
    name = str(name).strip()
    if name != DEFAULT_PROFILE:
        registry = _load_registry()
        if not any(p["name"] == name for p in registry):
            raise ValueError(f"Unknown profile {name!r}; register it first with add_profile()")
    path = _project_profile_path(project)
    try:
        _atomic_write(path, {"name": name})
    except OSError:
        pass


def config_dir_for(project: str) -> Path:
    """Return the ``CLAUDE_CONFIG_DIR`` path for *project*.

    Falls back to ``~/.claude`` for the default profile or any missing data.
    """
    name = profile_for(project)
    if name == DEFAULT_PROFILE:
        return _DEFAULT_CONFIG_DIR
    registry = _load_registry()
    for p in registry:
        if p["name"] == name:
            return Path(p["config_dir"])
    return _DEFAULT_CONFIG_DIR
