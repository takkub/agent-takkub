"""Dual-write: after a V1 config writer commits its own file, best-effort
mirror the same data into ``v2/`` (#362 Phase 10 wave 1 — "dual-write" step;
readers stay V1-only, switching them is wave 2's job).

**V1 stays authoritative.** Every caller here writes its own V1 file first,
on its own, synchronously and atomically — this module is only ever invoked
*after* that succeeds, and can never make a V1 save fail: every V2 write
below is wrapped so an ``OSError`` is logged (``v2_write_failed``) and
swallowed, never raised. The reverse order (V2 succeeds, V1 fails) cannot
happen by construction, since V1 always runs first and this module is only
reached once it already has.

**Skips silently on a not-yet-migrated machine** (no ``v2/`` root) — dual-
write only refreshes an EXISTING v2/ copy; creating one from scratch is
#361's job (`auto_migrate_boot` / `migrate apply`), not this module's.

**Callers pass already-loaded V1 data, not a path to re-read.** Every writer
below already has the exact payload it just persisted sitting in a local
variable at the point it calls into this module — passing that dict
straight through means dual-write can never disagree with V1 about what was
actually written (no risk of re-deriving the wrong path from
``config.SETTINGS_HOME``/``config.DATA_HOME`` when a caller — production or
test — has its own file living somewhere else). The one partial exception
is ``dual_write_routing``, whose V2 target is a merge of the global mapping
AND every known project's mapping — no single ``save_providers()`` call has
all of that in scope, so ``provider_config.py`` itself re-reads every scope
(through its own accessors) and hands the merged result in; this module
still never reaches into ``provider_config`` on its own (see that
function's docstring for why).

**V2 target paths are the one thing reused, never recomputed by hand** —
from ``core.migration.steps_v1``'s own ``RegistryMapping`` tuples (flat
registries) or its step classes' own target-path methods (the two fan-out
domains, role-agent and project). Reusing the exact same objects the ladder
itself is built from is what keeps this module and the ladder from ever
disagreeing about where a V1 file's V2 mirror lives. Only read-only
target-path methods are touched on those step instances — never
``apply()``/journal/backups, which would pollute the real migration
journal with per-save bookkeeping this isn't.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .layout import storage_layout_v2

_log = logging.getLogger(__name__)


def _effective_data_home(data_home: Path | None) -> Path:
    # Late, per-call import — NOT hoisted to module top — so this always
    # resolves through whatever `core.storage.layout.storage_layout_v2` is
    # bound to AT CALL TIME. `tests/conftest.py`'s autouse `_isolate_runtime`
    # fixture monkeypatches that module attribute (not `agent_takkub.config`
    # itself) to redirect the no-arg default off the real machine's
    # `DATA_HOME` — a module-level import here would bind the pre-patch
    # function once and silently keep resolving to the real data_home in
    # every test, the exact class of bug `core.routing.router` already
    # works around the same way for the same reason.
    from .layout import storage_layout_v2 as _storage_layout_v2

    return _storage_layout_v2(data_home).root.parent


def _v2_present(effective_data_home: Path) -> bool:
    return storage_layout_v2(effective_data_home).root.is_dir()


def _write_wrapped(name: str, target: Path, payload: dict) -> None:
    from ..migration.registry_copy_step import write_json_atomic

    try:
        write_json_atomic(target, payload)
    except OSError as e:
        _log.warning("v2_write_failed name=%r target=%r err=%s", name, target, e)


def _wrap(name: str, data: Any) -> dict:
    return {
        "schema": 1,
        "migrated_from": f"dual-write:{name}",
        "migrated_at": time.time(),
        "data": data,
    }


def _mapping_target(mappings, name: str) -> Path | None:
    return next((m.target for m in mappings if m.name == name), None)


def dual_write_role_models(entries: dict, *, data_home: Path | None = None) -> None:
    """Mirror ``role_models._save()``'s cleaned entries into
    ``models/aliases.json`` (ladder step 1's target)."""
    effective = _effective_data_home(data_home)
    if not _v2_present(effective):
        return
    from ..migration.steps_v1 import build_readonly_registries_step

    target = _mapping_target(
        build_readonly_registries_step(data_home=effective).mappings, "role-models"
    )
    if target is not None:
        _write_wrapped("role-models", target, _wrap("role-models", entries))


def dual_write_provider_models(models: dict, *, data_home: Path | None = None) -> None:
    """Mirror ``provider_models._save()``'s cleaned mapping into
    ``models/registry.json`` (ladder step 1's target)."""
    effective = _effective_data_home(data_home)
    if not _v2_present(effective):
        return
    from ..migration.steps_v1 import build_readonly_registries_step

    target = _mapping_target(
        build_readonly_registries_step(data_home=effective).mappings, "provider-models"
    )
    if target is not None:
        _write_wrapped("provider-models", target, _wrap("provider-models", models))


def dual_write_pane_tools_policy(file_payload: dict, *, data_home: Path | None = None) -> None:
    """Mirror ``pane_tools_policy.save_policy()``'s on-disk shape
    (``{"version": 1, "roles": {...}}``, or ``{}`` after a delete) into
    ``capabilities/mcp/permissions.json`` (ladder step 3's target)."""
    effective = _effective_data_home(data_home)
    if not _v2_present(effective):
        return
    from ..migration.steps_v1 import build_capability_step

    target = _mapping_target(build_capability_step(data_home=effective).mappings, "pane-tools")
    if target is not None:
        _write_wrapped("pane-tools", target, _wrap("pane-tools", file_payload))


def dual_write_skill_policy(file_payload: dict, *, data_home: Path | None = None) -> None:
    """Mirror ``skill_policy.save_policy()``'s on-disk shape into
    ``capabilities/skills/registry.json`` (ladder step 3's target)."""
    effective = _effective_data_home(data_home)
    if not _v2_present(effective):
        return
    from ..migration.steps_v1 import build_capability_step

    target = _mapping_target(build_capability_step(data_home=effective).mappings, "skill-policy")
    if target is not None:
        _write_wrapped("skill-policy", target, _wrap("skill-policy", file_payload))


def dual_write_routing(global_data: dict, projects: dict, *, data_home: Path | None = None) -> None:
    """Mirror role-providers.json — global + every known project — into
    ``config/routing.json`` (ladder step 2's target, `RoleAgentMigrationStep`
    shape: ``{"schema", "migrated_at", "global", "projects": {name: ...}}``).

    Unlike the flat registries above, one `provider_config.save_providers()`
    call only ever touches ONE scope (global or a single project), so
    `provider_config.py` itself re-reads every scope (through its own path
    accessors, `config_path` + `config.list_project_names()`) and hands the
    merged result straight in here — this module deliberately never imports
    `provider_config`: `config.py` (a leaf module under the
    `leaf-modules-pure` import-linter contract) imports THIS module for
    `dual_write_projects`, and `provider_config` transitively pulls in
    `provider_spec` -> `codex_helper`/`gemini_helper`, which would poison
    `config.py`'s own leaf status the moment this module reached for it.
    """
    effective = _effective_data_home(data_home)
    if not _v2_present(effective):
        return
    from ..migration.steps_v1 import RoleAgentMigrationStep

    payload = {
        "schema": 1,
        "migrated_at": time.time(),
        "global": global_data,
        "projects": projects,
    }
    target = RoleAgentMigrationStep(data_home=effective)._routing_target()
    _write_wrapped("role-providers", target, payload)


def dual_write_custom_roles_registry(file_payload: dict, *, data_home: Path | None = None) -> None:
    """Mirror ``custom_roles.save_custom_roles()``'s on-disk shape
    (``{"version": 1, "roles": {...}}``) into ``agents/custom/registry.json``
    (ladder step 2's target)."""
    effective = _effective_data_home(data_home)
    if not _v2_present(effective):
        return
    from ..migration.steps_v1 import RoleAgentMigrationStep

    target = RoleAgentMigrationStep(data_home=effective)._custom_roles_target()
    _write_wrapped("custom-roles", target, _wrap("custom-roles", file_payload))


def dual_write_custom_role_file(
    name: str, content: str | None, *, data_home: Path | None = None
) -> None:
    """Mirror one custom role's ``.md`` file verbatim into
    ``agents/custom/<name>.md``, or remove the V2 copy when ``content`` is
    ``None`` (the role's V1 file was just deleted)."""
    effective = _effective_data_home(data_home)
    if not _v2_present(effective):
        return
    target = storage_layout_v2(effective).agents / "custom" / f"{name}.md"
    try:
        if content is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(target)
    except OSError as e:
        _log.warning(
            "v2_write_failed name=%r target=%r err=%s", f"custom-role-md:{name}", target, e
        )


def dual_write_projects(data: dict, *, data_home: Path | None = None) -> None:
    """Mirror projects.json into ``projects/registry.json`` plus one
    ``projects/<id>/project.json`` per known project (ladder step 4's
    shape, worktree-ownership pointer included). ``data`` is the exact
    projects.json document the caller just wrote to V1."""
    effective = _effective_data_home(data_home)
    if not _v2_present(effective):
        return
    from ..migration.steps_v1 import ProjectMigrationStep

    step = ProjectMigrationStep(data_home=effective)
    _write_wrapped("projects-registry", step._registry_target(), _wrap("projects-registry", data))
    for pid, pdata in (data.get("projects") or {}).items():
        if not isinstance(pdata, dict):
            continue
        payload = _wrap(f"project:{pid}", pdata)
        payload["worktrees_owned"] = step._worktrees_owned(pid)
        _write_wrapped(f"project:{pid}", step._project_target(pid), payload)
