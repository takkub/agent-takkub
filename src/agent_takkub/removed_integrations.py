"""Integrations removed from the cockpit and the one-time boot cleanup of the
data they left behind — on EVERY install, not just the maintainer's machine.

Figma left the cockpit in 2.2.25–2.2.27, but a machine that had used it still
carries its MCP server entries, OAuth tokens, enabled plugin, plugin cache,
role MCP grants and skills under DATA_HOME. The 2026-10-22 cleanup was first
done by hand on one machine; this module ships it so everyone gets it.

Rules (same posture as :mod:`removed_providers`):

* Only cockpit-owned data under ``DATA_HOME`` — never a dev checkout, whose
  ``~/.claude`` is the developer's own config.
* Only entries inside MCP/plugin containers are dropped (``mcpServers``,
  ``mcpOAuth``, ``enabledPlugins`` …) — a project PATH that happens to contain
  the word is never touched.
* Every changed file is copied, every moved dir/file is moved, into
  ``DATA_HOME/backups/<name>-removed-<date>/`` first. Nothing is deleted.
* Runs once per integration (marker in ``system/migrations``), so a user who
  later adds the same MCP back on purpose keeps it.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from . import config

#: name -> release it left in. Add a name here when an integration is removed.
REMOVED_INTEGRATIONS: dict[str, str] = {"figma": "2.2.25"}

# Dicts whose KEYS are server/plugin/skill ids — a matching key is dropped.
_KEYED_CONTAINERS = frozenset(
    {"mcpServers", "mcpOAuth", "enabledPlugins", "plugins", "pluginUsage", "skillUsage"}
)
# Lists whose string ITEMS are server ids (role → MCP grants).
_LISTED_CONTAINERS = frozenset({"mcps"})


def _marker_path() -> Path:
    from .core.storage.layout import storage_layout_v2

    return storage_layout_v2().system_migrations / "removed-integrations.json"


def _scrub(obj: object, hit: Callable[[str], bool], container: str | None = None) -> object:
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if container in _KEYED_CONTAINERS and hit(key):
                continue
            out[key] = _scrub(value, hit, key)
        return out
    if isinstance(obj, list):
        if container in _LISTED_CONTAINERS:
            return [v for v in obj if not (isinstance(v, str) and hit(v))]
        return [_scrub(v, hit) for v in obj]
    return obj


def _json_targets(home: Path) -> list[tuple[Path, bool]]:
    """(file, top_level_is_keyed) — every cockpit-owned file a removed MCP
    can live in. `mcp-needs-auth-cache.json` is a bare {server: …} map."""
    claude = config.default_claude_config_dir()
    files: list[tuple[Path, bool]] = [
        (claude / ".claude.json", False),
        (claude / ".credentials.json", False),
        (claude / "settings.json", False),
        (claude / "plugins" / "installed_plugins.json", False),
        (claude / "mcp-needs-auth-cache.json", True),
        (home / "capabilities" / "mcp" / "permissions.json", False),
    ]
    files += [(p, False) for p in sorted((home / "runtime").glob("shared-mcp*.json"))]
    profiles = home / "providers" / "claude"
    if profiles.is_dir():
        for prof in sorted(p for p in profiles.iterdir() if p.is_dir()):
            files += [(prof / ".credentials.json", False), (prof / "settings.json", False)]
            files.append((prof / "mcp-needs-auth-cache.json", True))
    return files


def _move_targets(home: Path, name: str) -> list[Path]:
    claude = config.default_claude_config_dir()
    found = [home / "secrets" / f"{name}.json", *sorted((home / "skills").glob(f"{name}*"))]
    cache = claude / "plugins" / "cache"
    if cache.is_dir():  # <marketplace>/<plugin>/… — the plugin dir itself
        found += [d for d in sorted(cache.glob(f"*/{name}")) if d.is_dir()]
    return [p for p in found if p.exists()]


def _backup_rel(path: Path, home: Path) -> Path:
    try:
        return path.relative_to(home)
    except ValueError:
        return Path("external") / path.name


def cleanup_integration(name: str, *, home: Path | None = None) -> int:
    """Remove what integration *name* left behind; returns how many files
    were rewritten or moved. Idempotent — a clean machine returns 0."""
    home = home or config.DATA_HOME
    pattern = re.compile(re.escape(name), re.IGNORECASE)

    def hit(s: str) -> bool:
        return bool(pattern.search(s))

    backup = home / "backups" / f"{name}-removed-{time.strftime('%Y-%m-%d')}"
    changed = 0
    for path, top_keyed in _json_targets(home):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        cleaned = _scrub(data, hit, "mcpServers" if top_keyed else None)
        if cleaned == data:
            continue
        dst = backup / _backup_rel(path, home)
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dst)
        if config._write_json_atomic(path, cleaned):
            changed += 1
    for src in _move_targets(home, name):
        dst = backup / _backup_rel(src, home)
        if dst.exists():
            continue  # an earlier partial run already parked a copy — keep both safe
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        changed += 1
    return changed


def run_boot_cleanups() -> dict[str, int]:
    """Boot hook: clean up each removed integration once per install."""
    if config.DATA_HOME == config.REPO_ROOT:
        return {}  # dev checkout: ~/.claude is the developer's own
    marker = _marker_path()
    try:
        done = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        done = {}
    if not isinstance(done, dict):
        done = {}
    results: dict[str, int] = {}
    for name in REMOVED_INTEGRATIONS:
        if name in done:
            continue
        results[name] = cleanup_integration(name)
        done[name] = {"at": time.time(), "changed": results[name]}
    if results:
        marker.parent.mkdir(parents=True, exist_ok=True)
        config._write_json_atomic(marker, done)
    return results
