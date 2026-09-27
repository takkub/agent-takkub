"""Providers that were removed from the cockpit (#725) and what happens to
settings that still name them.

`kimi` left ``provider_spec.PROVIDER_REGISTRY`` in one pass. Stored settings
can outlive it, so this module does two things and nothing else:

* :func:`removed_provider_message` — the one user-facing sentence for
  ``takkub assign --provider kimi`` / ``--role kimi`` / a Settings pick, so
  nobody gets a traceback or a silent dead pane.
* :func:`migrate_removed_provider_settings` — boot-time value rewrite of the
  files that carry a role→provider mapping (``role-models`` aliases, per-project
  alias buckets, ``routing.json``). A ``kimi`` provider becomes the default
  provider (``claude``); its model/effort are dropped with it (a kimi model id
  means nothing to another CLI). Files are never deleted, only rewritten in
  place, and only when something actually changed.

The runtime already degrades without this (``provider_config`` ignores
providers it does not know), so the rewrite is hygiene, not load-bearing:
it keeps Settings/`takkub` output from showing a provider that no longer
exists. Path resolution goes through the same helpers the readers use, so it
covers dev (DATA_HOME == repo) and installed layouts alike.
"""

from __future__ import annotations

from typing import Any

#: name -> release note / issue tag. Add a name here when a provider is removed.
REMOVED_PROVIDERS: dict[str, str] = {"kimi": "#725"}

#: Where a removed provider's stored value lands.
DEFAULT_PROVIDER = "claude"


def is_removed_provider(name: object) -> bool:
    """True for a (case/whitespace-insensitive) removed provider or role name.
    A ``#N`` shard suffix is ignored, same as everywhere role names are read."""
    key = str(name or "").split("#", 1)[0].strip().lower()
    return key in REMOVED_PROVIDERS


def removed_provider_message(name: object) -> str:
    key = str(name or "").split("#", 1)[0].strip().lower()
    return (
        f"'{key}' provider ถูกถอดออกจาก cockpit แล้ว ({REMOVED_PROVIDERS.get(key, '')}) — "
        f"ใช้ provider อื่นแทน (เช่น claude/codex/gemini/opencode/cursor); "
        f"setting เดิมที่ชี้ {key} จะถูก fallback เป็น {DEFAULT_PROVIDER} อัตโนมัติ"
    )


def _rewrite_entries(bucket: Any) -> tuple[Any, int]:
    """``{role: {"provider": p, ...}}`` -> same with removed providers moved to
    the default. Returns ``(new_bucket, changed_count)``; a non-dict passes
    through untouched."""
    if not isinstance(bucket, dict):
        return bucket, 0
    out: dict = {}
    changed = 0
    for role, entry in bucket.items():
        if is_removed_provider(role):
            changed += 1  # the removed provider's own forced role: nothing to keep
            continue
        if isinstance(entry, dict) and is_removed_provider(entry.get("provider")):
            out[role] = {"provider": DEFAULT_PROVIDER}
            changed += 1
        else:
            out[role] = entry
    return out, changed


def _rewrite_provider_map(bucket: Any) -> tuple[Any, int]:
    """``{role: provider}`` (routing.json shape) variant of `_rewrite_entries`."""
    if not isinstance(bucket, dict):
        return bucket, 0
    out: dict = {}
    changed = 0
    for role, provider in bucket.items():
        if is_removed_provider(role):
            changed += 1
        elif is_removed_provider(provider):
            out[role] = DEFAULT_PROVIDER
            changed += 1
        else:
            out[role] = provider
    return out, changed


def _migrate_role_models() -> int:
    from . import role_models
    from .core.storage.v2_target import read_data, write_data

    total = 0
    data = read_data(role_models.path(), fresh=True)
    new, n = _rewrite_entries(data)
    if n:
        write_data(role_models.path(), new)
        total += n
    projects = read_data(role_models.projects_path(), fresh=True)
    if isinstance(projects, dict):
        new_projects: dict = {}
        n_all = 0
        for name, bucket in projects.items():
            new_bucket, n = _rewrite_entries(bucket)
            new_projects[name] = new_bucket
            n_all += n
        if n_all:
            write_data(role_models.projects_path(), new_projects)
            total += n_all
    return total


def _migrate_routing() -> int:
    from . import provider_config

    routing = provider_config._read_routing()
    global_data, n = _rewrite_provider_map(routing["global"])
    projects: dict = {}
    for name, bucket in routing["projects"].items():
        new_bucket, k = _rewrite_provider_map(bucket)
        projects[name] = new_bucket
        n += k
    if n:
        provider_config._write_routing(global_data, projects)
    return n


def migrate_removed_provider_settings() -> dict[str, int]:
    """Rewrite stored settings that name a removed provider. Idempotent and
    never raises (a failing store is skipped and reported as ``-1``): boot must
    not depend on it. Returns ``{store: values_rewritten}``."""
    result: dict[str, int] = {}
    for label, step in (("role_models", _migrate_role_models), ("routing", _migrate_routing)):
        try:
            result[label] = step()
        except Exception:
            result[label] = -1
    return result
