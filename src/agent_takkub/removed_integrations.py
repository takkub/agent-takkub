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
REMOVED_INTEGRATIONS: dict[str, str] = {"figma": "2.2.25", "ccc": "2.2.53"}

# Names matched EXACTLY (case-insensitive), not as a substring: "ccc" would
# otherwise catch any key that merely contains the letters.
_EXACT_NAMES = frozenset({"ccc"})


# Cockpit-written leftovers OUTSIDE the generic MCP/plugin containers.
# ccc (cocoindex-code semantic search, removed 2.2.53 — zero MCP calls in 90
# days of prod transcripts while it cost tool-schema tokens on every pane of
# an indexed project): its per-project index under DATA_HOME (15 GB on the
# owner's machine) and the global settings `takkub ccc index` wrote.
def _extra_move_targets(home: Path, name: str) -> list[Path]:
    if name == "ccc":
        return [home / "cocoindex-db", Path.home() / ".cocoindex_code"]
    return []


def _extra_json_targets(name: str) -> list[tuple[Path, bool]]:
    if name == "ccc":  # `takkub ccc provision` wrote `mcpServers.ccc` here
        return [(Path.home() / ".cursor" / "mcp.json", False)]
    return []


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
        if name in _EXACT_NAMES:
            return s.strip().lower() == name
        return bool(pattern.search(s))

    backup = home / "backups" / f"{name}-removed-{time.strftime('%Y-%m-%d')}"
    changed = 0
    for path, top_keyed in [*_json_targets(home), *_extra_json_targets(name)]:
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
    for src in [*_move_targets(home, name), *_extra_move_targets(home, name)]:
        if not src.exists():
            continue
        dst = backup / _backup_rel(src, home)
        if dst.exists():
            continue  # an earlier partial run already parked a copy — keep both safe
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        changed += 1
    return changed


#: marker key for the one-time uninstall of the plugins retired in 2.2.61.
RETIRED_PLUGINS_KEY = "retired-plugins-2.2.61"


def cleanup_retired_plugins(*, home: Path | None = None, claude_dir: Path | None = None) -> int:
    """Uninstall the plugins `plugin_installer.RETIRED` names from the
    cockpit's own Claude config, so every machine ends on the same base.

    Matched by exact id (``plugin@marketplace``) — never by substring, unlike
    `cleanup_integration`: "remember" or "code-review" inside some unrelated
    key must not be touched. Per plugin: its ``enabledPlugins`` /
    ``installed_plugins`` / usage entries and its cache dir. Per marketplace,
    only when no OTHER installed plugin still comes from it: the role grants
    naming it, its ``known_marketplaces`` entry, and its checkout. Everything
    changed is copied or moved to ``backups/retired-plugins-<date>/`` first.
    Returns the number of files rewritten or moved; 0 on a clean machine."""
    from .plugin_installer import RETIRED

    home = home or config.DATA_HOME
    claude = claude_dir or config.default_claude_config_dir()
    plugins_dir = claude / "plugins"
    retired_ids = {f"{p.key}@{p.marketplace}".lower() for p in RETIRED}
    backup = home / "backups" / f"retired-plugins-{time.strftime('%Y-%m-%d')}"
    changed = 0

    def _load(path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _save(path: Path, data: dict) -> None:
        nonlocal changed
        dst = backup / _backup_rel(path, home)
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dst)
        if config._write_json_atomic(path, data):
            changed += 1

    def _move(src: Path) -> None:
        nonlocal changed
        if not src.exists():
            return
        dst = backup / _backup_rel(src, home)
        if dst.exists():
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        changed += 1

    # Which marketplaces still have a plugin the user installed themselves.
    installed_path = plugins_dir / "installed_plugins.json"
    installed = _load(installed_path)
    kept_marketplaces: set[str] = set()
    if installed is not None and isinstance(installed.get("plugins"), dict):
        for key in installed["plugins"]:
            if isinstance(key, str) and key.lower() not in retired_ids:
                kept_marketplaces.add(key.partition("@")[2].lower())
        remaining = {k: v for k, v in installed["plugins"].items() if k.lower() not in retired_ids}
        if remaining != installed["plugins"]:
            _save(installed_path, {**installed, "plugins": remaining})
    gone_marketplaces = {p.marketplace.lower() for p in RETIRED} - kept_marketplaces

    settings_files = [claude / "settings.json"]
    profiles = home / "providers" / "claude"
    if profiles.is_dir():
        settings_files += [p / "settings.json" for p in sorted(profiles.iterdir()) if p.is_dir()]
    for path in settings_files:
        data = _load(path)
        if data is None:
            continue
        new = dict(data)
        for container in ("enabledPlugins", "pluginUsage"):
            block = data.get(container)
            if isinstance(block, dict):
                new[container] = {k: v for k, v in block.items() if k.lower() not in retired_ids}
        if new != data:
            _save(path, new)

    # Role grants are marketplace names (`roles.<role>.plugins`).
    perms_path = home / "capabilities" / "mcp" / "permissions.json"
    perms = _load(perms_path)
    if perms is not None and isinstance(perms.get("roles"), dict):
        roles = {}
        for role, entry in perms["roles"].items():
            if isinstance(entry, dict) and isinstance(entry.get("plugins"), list):
                entry = {
                    **entry,
                    "plugins": [
                        m
                        for m in entry["plugins"]
                        if not (isinstance(m, str) and m.lower() in gone_marketplaces)
                    ],
                }
            roles[role] = entry
        if roles != perms["roles"]:
            _save(perms_path, {**perms, "roles": roles})

    known_path = plugins_dir / "known_marketplaces.json"
    known = _load(known_path)
    if known is not None:
        new_known = {k: v for k, v in known.items() if k.lower() not in gone_marketplaces}
        if new_known != known:
            _save(known_path, new_known)

    for plugin in RETIRED:
        _move(plugins_dir / "cache" / plugin.marketplace / plugin.key)
    for marketplace in sorted({p.marketplace for p in RETIRED}):
        if marketplace.lower() not in gone_marketplaces:
            continue
        cache_dir = plugins_dir / "cache" / marketplace
        try:
            if cache_dir.is_dir() and not any(cache_dir.iterdir()):
                cache_dir.rmdir()  # emptied by the moves above
        except OSError:
            pass
        _move(plugins_dir / "marketplaces" / marketplace)
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
    if RETIRED_PLUGINS_KEY not in done:
        try:
            results[RETIRED_PLUGINS_KEY] = cleanup_retired_plugins()
        except Exception:  # never block boot; retried next start (no marker written)
            results.pop(RETIRED_PLUGINS_KEY, None)
        else:
            done[RETIRED_PLUGINS_KEY] = {
                "at": time.time(),
                "changed": results[RETIRED_PLUGINS_KEY],
            }
    if results:
        marker.parent.mkdir(parents=True, exist_ok=True)
        config._write_json_atomic(marker, done)
    return results
