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
import os
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from .config import SETTINGS_HOME as _BASE_DIR
from .config import default_claude_config_dir as _default_claude_config_dir

_REGISTRY_PATH = _BASE_DIR / "user-profiles.json"
# Installed builds isolate this under DATA_HOME (~/.agent-takkub/claude-config);
# dev checkouts keep the historical ~/.claude. See config.default_claude_config_dir.
_DEFAULT_CONFIG_DIR = _default_claude_config_dir()
DEFAULT_PROFILE = "default"

# Valid profile names: 1-64 chars, alphanumeric + hyphens + underscores,
# cannot be the reserved name "default".
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _project_slug(project: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", project)
    return slug if slug.strip(".") else "default"


def _project_profile_path(project: str) -> Path:
    return _BASE_DIR / "projects" / _project_slug(project) / "user-profile.json"


# Windows can transiently reject a rename onto *path* while another reader
# (e.g. SettingsWindow's background `_AccountsRefreshWorker`, which polls this
# same registry off the main thread — #518) still has it open for read. A
# short bounded retry absorbs that race without the caller ever seeing it —
# same shape as `config.py`'s `_write_json_atomic` / `editor_service.py`.
_REPLACE_RETRY_DELAYS: tuple[float, ...] = (0.0, 0.02, 0.05, 0.1, 0.2)


def _atomic_write(path: Path, data: object) -> None:
    """Write JSON to *path* atomically (tmp → rename).

    Raises ``OSError`` if the destination is still locked after every retry —
    callers must not swallow it silently (#518): a write that never lands
    leaves the user believing an add/remove succeeded when it didn't.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2) + "\n"
    # Write to a sibling temp file then rename for atomicity.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(text)
        last_exc: OSError | None = None
        for delay in _REPLACE_RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                Path(tmp).replace(path)
                return
            except OSError as exc:
                last_exc = exc
        assert last_exc is not None
        raise last_exc
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
        provider = normalize_provider(item.get("provider", "claude"))
        if name and name != DEFAULT_PROFILE:
            out.append({"name": name, "config_dir": config_dir, "provider": provider})
    return out


def list_profiles() -> list[dict]:
    """Return the implicit Claude default followed by registered profiles.

    Each entry: ``{"name": str, "config_dir": str, "provider": str}``.

    Keep the historical single-default shape because the Settings users page
    treats this as one flat, removable-profile list. Provider menus should use
    :func:`profiles_for_provider`, which synthesizes the appropriate default
    entry for each provider without making ``default`` appear removable.
    """
    registered = _load_registry()
    default_entry = {
        "name": DEFAULT_PROFILE,
        "config_dir": str(_DEFAULT_CONFIG_DIR),
        "provider": "claude",
    }
    return [default_entry, *registered]


_PROFILE_PROVIDER_ALIASES = {"openai": "codex"}


def normalize_provider(provider: object) -> str:
    """Return the canonical cockpit provider id used by profile mappings.

    Early multi-provider builds persisted ``openai`` while the runtime
    provider has always been named ``codex``. Read that spelling as a legacy
    alias so existing profile registries keep working, but only write the
    canonical id from now on.
    """
    name = str(provider or "claude").strip().lower() or "claude"
    return _PROFILE_PROVIDER_ALIASES.get(name, name)


def profiles_for_provider(provider: str) -> list[dict]:
    """Return one provider's implicit default followed by its profiles."""
    provider = normalize_provider(provider)
    default_dir = str(_DEFAULT_CONFIG_DIR) if provider == "claude" else ""
    default_entry = {
        "name": DEFAULT_PROFILE,
        "config_dir": default_dir,
        "provider": provider,
    }
    return [default_entry, *[p for p in _load_registry() if p["provider"] == provider]]


# Items shared with the default profile when a profile is created with
# share_sessions=True ("สลับเฉพาะบัญชี — session เดิมอยู่ครบ"):
#   projects/ — Claude Code transcripts + resume state (the actual sessions)
#   todos/    — per-session todo state
#   plugins/  — installed plugin cache (skills keep working)
#   skills/   — user-level skills
# Directories become junctions (win) / symlinks (posix) into ~/.claude, so BOTH
# profiles literally read and write the same session store. Credentials,
# settings.json, .claude.json, statsig/ stay per-profile — that's the account.
SHARED_ITEMS: tuple[str, ...] = ("projects", "todos", "plugins", "skills")


def provision_shared_profile(config_dir: str | Path, share_from: Path | None = None) -> list[str]:
    """Create *config_dir* as a shared-session profile home.

    Links each :data:`SHARED_ITEMS` dir from *share_from* (default
    ``~/.claude``) into *config_dir*. Missing source dirs are created first so
    the link target is always valid. Existing destination entries are left
    untouched (never clobbered). Returns the list of item names linked.
    Raises ``OSError`` only when the profile dir itself cannot be created.
    """
    from .worktree_manager import _make_link

    src_home = Path(share_from) if share_from else _DEFAULT_CONFIG_DIR
    dest_home = Path(config_dir).expanduser()
    dest_home.mkdir(parents=True, exist_ok=True)
    linked: list[str] = []
    for item in SHARED_ITEMS:
        src = src_home / item
        dst = dest_home / item
        if dst.exists() or dst.is_symlink():
            continue  # never clobber whatever is already there
        try:
            src.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        if _make_link(src, dst) is None:
            linked.append(item)
    return linked


def _merge_tree(src: Path, dst: Path) -> tuple[int, int]:
    """Copy every file under *src* into *dst* that doesn't already exist there
    (never overwrites — existing files win). Returns (copied, skipped)."""
    import shutil as _shutil

    copied = skipped = 0
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        target = dst / rel
        if target.exists():
            skipped += 1
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _shutil.copy2(p, target)
            copied += 1
        except OSError:
            skipped += 1
    return copied, skipped


def convert_profile_to_shared(
    config_dir: str | Path, share_from: Path | None = None
) -> dict[str, str]:
    """Convert an EXISTING profile dir (already-split data) to shared-session.

    Per :data:`SHARED_ITEMS` item: merge the profile's own files into the
    default home (existing files there win — nothing is overwritten), rename
    the original dir to ``<item>.pre-share-backup`` (kept — user deletes it
    when confident), then link the default home's dir in its place. Items that
    are already link points are skipped (idempotent). Returns
    {item: summary} for display.
    """
    from .worktree_manager import _is_link_point, _make_link

    src_home = Path(share_from) if share_from else _DEFAULT_CONFIG_DIR
    dest_home = Path(config_dir).expanduser()
    results: dict[str, str] = {}
    for item in SHARED_ITEMS:
        p = dest_home / item
        if _is_link_point(p):
            results[item] = "already shared"
            continue
        try:
            main = src_home / item
            main.mkdir(parents=True, exist_ok=True)
            if p.is_dir():
                copied, skipped = _merge_tree(p, main)
                backup = dest_home / f"{item}.pre-share-backup"
                n = 1
                while backup.exists():
                    n += 1
                    backup = dest_home / f"{item}.pre-share-backup{n}"
                p.rename(backup)
                note = f"merged {copied} file(s) in"
                if skipped:
                    note += f", {skipped} already present"
                note += f" · original kept as {backup.name}"
            else:
                note = "created"
            err = _make_link(main, p)
            results[item] = note if err is None else f"link failed: {err}"
        except OSError as e:
            results[item] = f"failed: {e}"
    return results


def cleanup_profile_links(config_dir: str | Path) -> list[str]:
    """Remove the shared-item LINK POINTS under *config_dir* (never their
    targets, never real directories). Call before a profile dir is deleted so
    a recursive delete by the user/Explorer cannot traverse a junction and
    wipe the shared ~/.claude session store. Returns removed item names."""
    from .worktree_manager import _is_link_point, _remove_link

    dest_home = Path(config_dir).expanduser()
    removed: list[str] = []
    for item in SHARED_ITEMS:
        p = dest_home / item
        try:
            if p.exists() or p.is_symlink():
                if _is_link_point(p):
                    _remove_link(p)
                    removed.append(item)
        except OSError:
            continue
    return removed


def add_profile(
    name: str, config_dir: str | Path = "", share_sessions: bool = False, provider: str = "claude"
) -> list[str]:
    """Register a new profile.

    ``share_sessions=True`` provisions *config_dir* so sessions/plugins are
    shared with the default profile (see :func:`provision_shared_profile`) —
    switching users changes ONLY the login/credentials. Returns the list of
    shared items linked ([] when not sharing). (Only applies to 'claude' provider).

    Raises ``ValueError`` if *name* is invalid or already taken, or
    ``OSError`` if the registry file could not be persisted (#518 — a
    transient lock that outlasted `_atomic_write`'s retries, or a real disk
    error). Callers must surface both to the user rather than assume success.
    """
    name = str(name).strip()
    provider = normalize_provider(provider)
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

    linked: list[str] = []
    if share_sessions and provider == "claude":
        try:
            linked = provision_shared_profile(config_dir_s)
        except OSError as e:
            raise ValueError(f"Cannot create profile dir {config_dir_s}: {e}") from e

    profiles.append({"name": name, "config_dir": config_dir_s, "provider": provider})
    _atomic_write(_REGISTRY_PATH, profiles)
    return linked


def remove_profile(name: str) -> None:
    """Remove a registered profile by name.

    Silent if not found. Raises ``ValueError`` for the reserved default, or
    ``OSError`` if the registry file could not be persisted (#518 — see
    :func:`add_profile`).
    """
    name = str(name).strip()
    if name == DEFAULT_PROFILE:
        raise ValueError("Cannot remove the implicit 'default' profile")
    profiles = _load_registry()
    updated = [p for p in profiles if p["name"] != name]
    _atomic_write(_REGISTRY_PATH, updated)


def profile_for(project: str, provider: str = "claude") -> str:
    """Return the profile name selected for *project* and *provider* (``"default"`` if unset)."""
    provider = normalize_provider(provider)
    path = _project_profile_path(project)
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return DEFAULT_PROFILE
    if not isinstance(data, dict):
        return DEFAULT_PROFILE

    # Backward compatibility: old format was {"name": "profile_name"} meaning claude.
    # New format: {"providers": {"claude": "profile_name", "codex": "default"}, "default_provider": "claude"}
    if "name" in data and "providers" not in data:
        if provider == "claude":
            name = str(data.get("name", "")).strip()
        else:
            name = DEFAULT_PROFILE
    else:
        providers_dict = data.get("providers", {})
        if not isinstance(providers_dict, dict):
            return DEFAULT_PROFILE
        # Compatibility with the short-lived "openai" spelling.
        if provider == "codex" and "codex" not in providers_dict:
            name = str(providers_dict.get("openai", DEFAULT_PROFILE)).strip()
        else:
            name = str(providers_dict.get(provider, DEFAULT_PROFILE)).strip()

    if not name:
        return DEFAULT_PROFILE
    # Verify the name still exists in the registry for this provider
    if name != DEFAULT_PROFILE:
        registry = _load_registry()
        if not any(p["name"] == name and p["provider"] == provider for p in registry):
            return DEFAULT_PROFILE
    return name


def set_profile(project: str, name: str, provider: str = "claude") -> None:
    """Assign a profile to *project* for a specific *provider*.

    Raises ``ValueError`` if *name* is not in the registry for that provider (and not
    ``"default"``).  Silent on I/O errors.
    """
    name = str(name).strip()
    provider = normalize_provider(provider)
    if name != DEFAULT_PROFILE:
        registry = _load_registry()
        if not any(p["name"] == name and p["provider"] == provider for p in registry):
            raise ValueError(
                f"Unknown profile {name!r} for provider {provider!r}; register it first with add_profile()"
            )

    path = _project_profile_path(project)
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}

    # Migrate old format to new format
    if "name" in data and "providers" not in data:
        old_claude = data.pop("name")
        data["providers"] = {"claude": old_claude}

    if "providers" not in data:
        data["providers"] = {}

    if isinstance(data["providers"], dict) and "openai" in data["providers"]:
        data["providers"].setdefault("codex", data["providers"].pop("openai"))

    data["providers"][provider] = name

    try:
        _atomic_write(path, data)
    except OSError:
        pass


def default_provider(project: str) -> str:
    """Return the active provider selected for *project* (defaults to "claude")."""
    path = _project_profile_path(project)
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return "claude"
    if not isinstance(data, dict):
        return "claude"
    provider = normalize_provider(data.get("default_provider", "claude"))
    from .provider_config import VALID_PROVIDERS

    return provider if provider in VALID_PROVIDERS else "claude"


def set_default_provider(project: str, provider: str) -> None:
    """Set the active provider for *project*."""
    provider = normalize_provider(provider)
    from .provider_config import VALID_PROVIDERS

    if provider not in VALID_PROVIDERS:
        raise ValueError(f"Unknown provider {provider!r}")
    path = _project_profile_path(project)
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}

    if "name" in data and "providers" not in data:
        old_claude = data.pop("name")
        data["providers"] = {"claude": old_claude}

    data["default_provider"] = provider

    try:
        _atomic_write(path, data)
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


def _mirror_file(src: Path, dst: Path) -> None:
    """Keep *dst* in sync with *src* via hardlink (falling back to copy)."""
    if not src.is_file():
        if dst.exists() or dst.is_symlink():
            try:
                dst.unlink(missing_ok=True)
            except OSError:
                pass
        return
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() or dst.is_symlink():
            try:
                s_stat = src.stat()
                d_stat = dst.stat()
                if s_stat.st_ino != 0 and (s_stat.st_ino, s_stat.st_dev) == (
                    d_stat.st_ino,
                    d_stat.st_dev,
                ):
                    return
                if s_stat.st_mtime == d_stat.st_mtime and s_stat.st_size == d_stat.st_size:
                    return
            except OSError:
                pass
            dst.unlink(missing_ok=True)
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)
    except OSError:
        pass


def _extract_skill_name(path: Path) -> str:
    from . import skill_scan

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        fm = skill_scan._parse_frontmatter(text)
        name = fm.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    except OSError:
        pass
    return path.parent.name if path.name == "SKILL.md" else path.stem


def _find_plugin_dir(base_dir: Path, plugin_key: str, dest_dir: Path | None = None) -> Path | None:
    """Locate the root directory for *plugin_key* (e.g. ``name@marketplace`` or ``name``).

    Searches:
    1. ``installed_plugins.json`` in base_dir / dest_dir
    2. ``plugins/cache/<marketplace>/<name>`` (version subdirs or direct)
    3. ``plugins/marketplaces/<marketplace>/plugins/<name>``
    4. ``plugins/marketplaces/<marketplace>``
    5. ``plugins/<name>``
    """
    search_dirs: list[Path] = [base_dir / "plugins"]
    if dest_dir is not None and dest_dir != base_dir:
        search_dirs.append(dest_dir / "plugins")

    for pdir in search_dirs:
        installed_file = pdir / "installed_plugins.json"
        if installed_file.is_file():
            try:
                data = json.loads(installed_file.read_text(encoding="utf-8"))
                entries = data.get("plugins", {}).get(plugin_key, [])
                if isinstance(entries, list) and entries:
                    ip = entries[0].get("installPath")
                    if ip:
                        p = Path(ip)
                        if p.is_dir():
                            return p
            except Exception:
                pass

    name, _, marketplace = plugin_key.partition("@")

    for pdir in search_dirs:
        if marketplace:
            cache_dir = pdir / "cache" / marketplace / name
            if cache_dir.is_dir():
                try:
                    versions = sorted((v for v in cache_dir.iterdir() if v.is_dir()), reverse=True)
                    if versions:
                        return versions[0]
                except OSError:
                    pass
                return cache_dir

            mp_plugin = pdir / "marketplaces" / marketplace / "plugins" / name
            if mp_plugin.is_dir():
                return mp_plugin

            mp_root = pdir / "marketplaces" / marketplace
            if mp_root.is_dir():
                return mp_root

        direct = pdir / name
        if direct.is_dir():
            return direct

        cache_base = pdir / "cache"
        if cache_base.is_dir():
            try:
                for mp in cache_base.iterdir():
                    if mp.is_dir():
                        cand = mp / name
                        if cand.is_dir():
                            versions = sorted(
                                (v for v in cand.iterdir() if v.is_dir()), reverse=True
                            )
                            return versions[0] if versions else cand
            except OSError:
                pass

        mp_base = pdir / "marketplaces"
        if mp_base.is_dir():
            try:
                for mp in mp_base.iterdir():
                    if mp.is_dir():
                        cand = mp / "plugins" / name
                        if cand.is_dir():
                            return cand
            except OSError:
                pass

    return None


def _extract_plugin_skills(plugin_dir: Path) -> set[str]:
    """Return set of skill names provided by the plugin at *plugin_dir*.

    Checks:
    1. Manifest (``.claude-plugin/plugin.json`` or ``plugin.json``) for a ``skills`` path
    2. Standard ``skills`` or ``.claude/skills`` directories
    Returns empty set if no skills are defined or skills are explicitly disabled.
    """
    if not plugin_dir.is_dir():
        return set()

    skills_dirs: list[Path] = []
    manifest = plugin_dir / ".claude-plugin" / "plugin.json"
    if not manifest.is_file():
        manifest = plugin_dir / "plugin.json"

    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            skills_val = data.get("skills")
            if skills_val is False:
                return set()
            if isinstance(skills_val, str) and skills_val.strip():
                custom_dir = (plugin_dir / skills_val.strip()).resolve()
                if custom_dir.is_dir():
                    skills_dirs.append(custom_dir)
        except Exception:
            pass

    if not skills_dirs:
        for candidate_name in ("skills", ".claude/skills"):
            candidate = plugin_dir / candidate_name
            if candidate.is_dir():
                skills_dirs.append(candidate)

    from .skill_scan import _skill_files

    skill_names: set[str] = set()
    for sdir in skills_dirs:
        try:
            for f in _skill_files(sdir):
                sname = _extract_skill_name(f)
                if sname:
                    skill_names.add(sname)
                skill_names.add(f.parent.name if f.name == "SKILL.md" else f.stem)
        except OSError:
            pass
    return skill_names


def _curate_settings_plugins(
    base_dir: Path,
    dest_dir: Path,
    allowed_names: set[str],
) -> None:
    """Rewrite curated settings.json enabledPlugins to only keep assigned plugins (#580).

    Rules:
    - Never remove plugins that do not provide skills (e.g. MCP / hooks only).
    - If metadata cannot be resolved, preserve the plugin (do not guess).
    - For plugins providing skills, keep only those whose skills or identifiers
      are in allowed_names.
    """
    src_settings = base_dir / "settings.json"
    dst_settings = dest_dir / "settings.json"

    target_read = src_settings if src_settings.is_file() else dst_settings
    if not target_read.is_file():
        return

    try:
        data = json.loads(target_read.read_text(encoding="utf-8"))
    except Exception:
        return

    enabled_plugins = data.get("enabledPlugins")
    if not isinstance(enabled_plugins, dict):
        return

    curated_plugins: dict[str, object] = {}
    for plugin_key, val in enabled_plugins.items():
        plugin_dir = _find_plugin_dir(base_dir, plugin_key, dest_dir=dest_dir)
        if plugin_dir is not None:
            skills = _extract_plugin_skills(plugin_dir)
            if not skills:
                # Plugin does not provide skills (e.g. MCP / hooks only) -> keep
                curated_plugins[plugin_key] = val
                continue
        else:
            # Metadata could not be determined; per spec: do not guess, preserve
            curated_plugins[plugin_key] = val
            continue

        # Plugin provides skills: keep only if assigned
        name, _, mp = plugin_key.partition("@")
        identifiers = {plugin_key, name}
        if mp:
            identifiers.add(mp)

        if bool(skills & allowed_names) or bool(identifiers & allowed_names):
            curated_plugins[plugin_key] = val

    data["enabledPlugins"] = curated_plugins

    try:
        if dst_settings.exists() or dst_settings.is_symlink():
            dst_settings.unlink(missing_ok=True)
        dst_settings.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def curated_config_dir_for(project: str) -> Path:
    """Return the curated ``CLAUDE_CONFIG_DIR`` path for *project* under DATA_HOME (#563).

    Follows #504's layout: ``DATA_HOME/providers/claude/<account>-<slug>``.
    """
    from .config import DATA_HOME

    account = profile_for(project)
    slug = _project_slug(project)
    return DATA_HOME / "providers" / "claude" / f"{account}-{slug}"


def ensure_curated_claude_config_dir(project: str, base_dir: Path | None = None) -> Path:
    """Prepare a curated ``CLAUDE_CONFIG_DIR`` for *project* (#563, #580).

    Mirrors auth credentials (.credentials.json) and settings from the base
    profile, links session stores (projects, todos, plugins), populates
    a curated skills/ directory containing only project-owned skills and
    skills assigned in the Skill Matrix policy, and filters enabledPlugins
    in settings.json to include only assigned plugins and non-skill plugins.
    """
    from . import skill_policy, skill_scan
    from .lead_context import _allowed_project_roots
    from .worktree_manager import _is_link_point, _make_link, _remove_link

    if base_dir is None:
        base_dir = config_dir_for(project)
    dest_dir = curated_config_dir_for(project)
    try:
        if dest_dir.resolve() == base_dir.resolve():
            return dest_dir
    except OSError:
        pass

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return base_dir

    # 1. Mirror auth credentials and user settings
    for fname in (
        ".credentials.json",
        "settings.json",
        "settings.local.json",
        "keybindings.json",
        "CLAUDE.md",
    ):
        _mirror_file(base_dir / fname, dest_dir / fname)

    # 2. Link shared session stores
    for dirname in ("projects", "todos", "plugins"):
        src = base_dir / dirname
        dst = dest_dir / dirname
        if src.is_dir() and not (dst.exists() or dst.is_symlink()):
            _make_link(src, dst)

    # 3. Curate skills directory
    base_skills = base_dir / "skills"
    curated_skills = dest_dir / "skills"
    try:
        curated_skills.mkdir(parents=True, exist_ok=True)
    except OSError:
        return dest_dir

    # Collect allowed skill names for this project
    allowed_names: set[str] = set()
    try:
        roots = _allowed_project_roots(project)
        allowed_names.update(s.name for s in skill_scan.scan_skills(roots))
    except Exception:
        pass
    try:
        for r_skills in skill_policy.load_policy().values():
            allowed_names.update(r_skills)
    except Exception:
        pass

    gate_active = (
        os.environ.get("TAKKUB_SKILL_GATE", "1").strip() != "0"
        and bool(project)
        and project != "default"
    )

    # Copy / link matching skills from base_skills
    if base_skills.is_dir():
        try:
            for entry in base_skills.iterdir():
                if entry.is_dir():
                    md_file = entry / "SKILL.md"
                    if not md_file.is_file():
                        mds = list(entry.glob("*.md"))
                        md_file = mds[0] if mds else None
                    if md_file is not None:
                        sname = _extract_skill_name(md_file)
                    else:
                        sname = entry.name
                    if not gate_active or sname in allowed_names or entry.name in allowed_names:
                        dst_skill = curated_skills / entry.name
                        if not (dst_skill.exists() or dst_skill.is_symlink()):
                            _make_link(entry, dst_skill)
                elif entry.is_file() and entry.suffix == ".md":
                    sname = _extract_skill_name(entry)
                    if not gate_active or sname in allowed_names or entry.stem in allowed_names:
                        _mirror_file(entry, curated_skills / entry.name)
        except OSError:
            pass

    # Prune non-allowed skills from curated_skills
    try:
        for existing in curated_skills.iterdir():
            sname = ""
            if existing.is_dir():
                md_file = existing / "SKILL.md"
                if not md_file.is_file():
                    mds = list(existing.glob("*.md"))
                    md_file = mds[0] if mds else None
                sname = _extract_skill_name(md_file) if md_file else existing.name
            elif existing.is_file() and existing.suffix == ".md":
                sname = _extract_skill_name(existing)

            if gate_active and (
                sname not in allowed_names
                and existing.name not in allowed_names
                and existing.stem not in allowed_names
            ):
                if _is_link_point(existing):
                    _remove_link(existing)
                elif existing.is_file() or existing.is_symlink():
                    existing.unlink(missing_ok=True)
                elif existing.is_dir():
                    shutil.rmtree(existing, ignore_errors=True)
    except OSError:
        pass

    # 4. Curate plugins in settings.json (#580)
    if gate_active:
        _curate_settings_plugins(base_dir, dest_dir, allowed_names)

    return dest_dir


# ── First-boot profile clone (installed instances only) ─────────────────────
#
# ~/.claude can be multiple GB (projects/ transcripts, security/, plugins
# cache, shell-snapshots/, file-history/, ...). A synchronous full-tree copy
# of that on the main thread before the window paints was observed blocking
# startup 8+ minutes on a 2.9GB real-world profile — the window never
# appeared, the user assumed the app was dead, and a second launch's
# auto-kill couldn't even kill the wedged (I/O-bound) process. The fix:
#   1. Allowlist — clone only what gives a NEW profile a head start (config,
#      skills, agents, plugins), plus a bounded number of each project's most
#      recent session transcripts (see `_clone_recent_sessions`) so
#      chatlog_scanner/resume aren't starting from a totally empty slate.
#      An allowlist (not a denylist) means any future/unrecognized
#      ~/.claude item defaults to "don't copy" instead of silently
#      ballooning the copy again.
#   2. Atomicity — build the clone in a `.partial` sibling dir, write a
#      completion marker, then a single `os.replace()` into place. A kill
#      mid-copy leaves only the `.partial` (cleaned up on the next boot),
#      never a half-written `dest`.
_BOOTSTRAP_CORE_ITEMS: tuple[str, ...] = (
    "CLAUDE.md",
    "settings.json",
    "settings.local.json",
    "keybindings.json",
    "agents",
    "commands",
    "skills",
    "plugins",
)
_BOOTSTRAP_MARKER = ".bootstrap-complete"

# Per-project cap on recent session transcripts cloned (not a total cap —
# each project subdir under ~/.claude/projects/ gets its own most-recent N).
RECENT_SESSIONS_CLONE = 10
# Single-session-file safety valve: an abnormally large transcript is
# skipped rather than cloned, so one giant session can't turn a "cheap
# bounded copy" back into the slow unbounded copy this fix exists to avoid.
_RECENT_SESSION_MAX_BYTES = 50 * 1024 * 1024


def _clone_core_items(src: Path, dest: Path) -> None:
    for name in _BOOTSTRAP_CORE_ITEMS:
        s = src / name
        if not s.exists():
            continue
        d = dest / name
        try:
            if s.is_dir():
                shutil.copytree(s, d)
            else:
                shutil.copy2(s, d)
        except OSError:
            continue


def _clone_recent_sessions(src: Path, dest: Path) -> int:
    """Copy the :data:`RECENT_SESSIONS_CLONE` most-recently-modified
    ``*.jsonl`` session files from EACH project subdir under
    ``src/projects/`` into ``dest/projects/``, preserving the
    ``<encoded-cwd>/<session>.jsonl`` layout so chatlog_scanner/resume find
    them without any special-casing. A file over
    :data:`_RECENT_SESSION_MAX_BYTES` is skipped rather than counted/copied
    (ponytail: no backfill from the 11th-most-recent file when one of the
    top N is skipped for size — simplest behavior that satisfies "bounded,
    cheap, and safe"). Returns the number of oversized files skipped.
    """
    projects_src = src / "projects"
    if not projects_src.is_dir():
        return 0
    skipped_oversized = 0
    for project_dir in projects_src.iterdir():
        if not project_dir.is_dir():
            continue
        sessions = sorted(
            (p for p in project_dir.glob("*.jsonl") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for p in sessions[:RECENT_SESSIONS_CLONE]:
            try:
                if p.stat().st_size > _RECENT_SESSION_MAX_BYTES:
                    skipped_oversized += 1
                    continue
                target = dest / "projects" / project_dir.name / p.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, target)
            except OSError:
                continue
    return skipped_oversized


def bootstrap_default_profile(
    log_event: Callable[..., None] | None = None,
) -> bool:
    """First-boot only: give an installed instance's default Claude profile
    (``DATA_HOME/claude-config``) a head start by cloning a small allowlist
    of items from the user's existing ``~/.claude``: CLAUDE.md, settings,
    keybindings, agents/commands/skills/plugins, plus each project's most
    recent session transcripts (see :data:`RECENT_SESSIONS_CLONE`). Never
    copies ``.credentials.json`` (login is per-instance).

    No-op for dev checkouts (default profile already IS ``~/.claude``).
    *dest* is left completely untouched if it already holds a real profile —
    either the completion marker from a prior successful bootstrap, or
    ``.credentials.json`` proving the user has logged in there. A *dest*
    that exists with neither (torn: a `.partial` promoted mid-copy by a
    killed process, or a stray dir with no real user data — never logged
    into) is discarded and re-cloned, since there is nothing of the user's
    to lose.

    *log_event* (if given) is called with
    ``"profile_recent_sessions_oversized_skipped"`` and a ``count`` kwarg
    when one or more session files were skipped for size.

    Returns True iff a clone actually happened.
    """
    from .config import DATA_HOME, REPO_ROOT

    if DATA_HOME == REPO_ROOT:
        return False
    dest = _DEFAULT_CONFIG_DIR
    if dest.exists():
        if (dest / _BOOTSTRAP_MARKER).exists() or (dest / ".credentials.json").exists():
            return False  # real profile — never touch
        if sys.platform == "darwin":
            from .limit_status import _read_keychain_credentials

            if _read_keychain_credentials():
                return False  # logged-in macOS profile — credentials live in Keychain
        shutil.rmtree(dest, ignore_errors=True)  # torn from an earlier interrupted clone

    src = Path.home() / ".claude"
    if not src.is_dir():
        return False

    partial = dest.with_name(dest.name + ".partial")
    shutil.rmtree(partial, ignore_errors=True)  # stale partial from a killed prior attempt
    try:
        partial.mkdir(parents=True)
    except OSError:
        return False

    _clone_core_items(src, partial)
    skipped_oversized = _clone_recent_sessions(src, partial)
    if skipped_oversized and log_event is not None:
        try:
            log_event("profile_recent_sessions_oversized_skipped", count=skipped_oversized)
        except Exception:
            pass

    try:
        (partial / _BOOTSTRAP_MARKER).write_text("", encoding="utf-8")
        os.replace(partial, dest)
    except OSError:
        shutil.rmtree(partial, ignore_errors=True)
        return False
    return True
