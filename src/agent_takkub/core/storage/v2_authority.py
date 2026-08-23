"""V2-authoritative readers (#362 Phase 10 wave 2 — "readers" step; wave 1
dual-write, ``core.storage.dual_write``, already mirrors every domain below
into ``v2/`` on every V1 write).

Gated by :func:`v2_authority_enabled` (env ``TAKKUB_V2_AUTHORITY``, else the
Core V2 Settings page's ``v2_authority`` flag — same env-wins-else-Settings
precedence as ``core.routing.flag.v2_router_enabled``). **Default OFF** —
unlike the other `TAKKUB_V2_*` flags (default-on since 1.0.84), flipping this
one's default is a 2.0.0 release decision made after a drift-free soak, not
something this module decides. While OFF every function below still exists
and is unit-tested, but no V1 module's loader calls into it.

**Fail-open contract** (mirrors ``core.storage.dual_write``'s the other way
round): flag ON + ``v2/`` absent (never migrated) -> ``None``, silent — the
expected state on every unmigrated machine, not a signal. Flag ON + ``v2/``
present but the mirror file is missing/corrupt/an unexpected shape -> also
``None``, but logged as ``v2_read_failed`` (dual-write should have kept it in
sync, so this IS worth knowing about). Either way the caller falls back to
its own V1 read; nothing here ever raises.

**Why a domain's shape needs no re-sanitization**: every ``dual_write_*``
writer is called with the exact payload its V1 sibling just persisted to its
own file — already validated by that writer's own rules. Reading it back
(after an isinstance check against the expected container shape) reproduces
what ``_load()``/``load_policy()``/etc. would have produced from equivalent
V1 JSON, without re-running each domain's validation a second time. A caller
that still wants defense-in-depth (role/name allowlists, provider-registry
membership, ...) runs its OWN sanitizer over the returned dict/list exactly
as it would over freshly-parsed V1 JSON — this module only answers "does a
usable V2 mirror exist", never "is it semantically valid for my domain".

**Not covered here** (dual-write mirrors them; no reader to redirect):
``exec_mode``/``auto_resume``/``rtk_helper``. Their public getters
(``current()``, ``is_enabled()``, ``rtk_hook_enabled()``) are hardcoded
constants that never re-read their own JSON state at all — there is no V1
read call site here to switch, so wave 1's dual-write mirror is already the
complete story for those three domains.

**Target-path reuse**: every path below is resolved the exact same way
``core.storage.dual_write`` resolves its write target — through that
module's own ``_effective_data_home``/``_v2_present``/``_mapping_target``
helpers, imported directly rather than re-implemented, so a reader and its
writer can never disagree about where a domain's mirror lives.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .dual_write import _effective_data_home, _mapping_target, _v2_present
from .legacy_reader import read_json

_log = logging.getLogger(__name__)


def v2_authority_enabled() -> bool:
    raw = os.environ.get("TAKKUB_V2_AUTHORITY")
    if raw is not None:
        return raw == "1"
    from agent_takkub import core_v2_settings

    return core_v2_settings.flag_enabled("v2_authority")


def _read_data(name: str, target: Path | None) -> Any | None:
    """Unwrap a dual-write mirror's ``.data`` field. ``None`` on a missing
    target (silent) or a present-but-unreadable/unwrapped one (logged)."""
    if target is None or not target.exists():
        return None
    raw = read_json(target)
    if not raw or "data" not in raw:
        _log.warning("v2_read_failed name=%r target=%r err=malformed-or-empty", name, target)
        return None
    return raw["data"]


def _read_raw(name: str, target: Path | None) -> dict | None:
    """Same as :func:`_read_data` but for the one target dual-write writes
    with no ``.data`` wrapper (``role-providers`` routing) — returns the raw
    JSON dict verbatim."""
    if target is None or not target.exists():
        return None
    raw = read_json(target)
    if not raw:
        _log.warning("v2_read_failed name=%r target=%r err=malformed-or-empty", name, target)
        return None
    return raw


def read_role_models(*, data_home: Path | None = None) -> dict | None:
    effective = _effective_data_home(data_home)
    if not _v2_present(effective):
        return None
    from ..migration.steps_v1 import build_readonly_registries_step

    target = _mapping_target(
        build_readonly_registries_step(data_home=effective).mappings, "role-models"
    )
    data = _read_data("role-models", target)
    return data if isinstance(data, dict) else None


def read_provider_models(*, data_home: Path | None = None) -> dict | None:
    effective = _effective_data_home(data_home)
    if not _v2_present(effective):
        return None
    from ..migration.steps_v1 import build_readonly_registries_step

    target = _mapping_target(
        build_readonly_registries_step(data_home=effective).mappings, "provider-models"
    )
    data = _read_data("provider-models", target)
    return data if isinstance(data, dict) else None


def read_disabled_providers(*, data_home: Path | None = None) -> dict | None:
    effective = _effective_data_home(data_home)
    if not _v2_present(effective):
        return None
    from ..migration.steps_v1 import build_readonly_registries_step

    target = _mapping_target(
        build_readonly_registries_step(data_home=effective).mappings, "disabled-providers"
    )
    data = _read_data("disabled-providers", target)
    return data if isinstance(data, dict) else None


def read_pane_tools_policy(*, data_home: Path | None = None) -> dict | None:
    effective = _effective_data_home(data_home)
    if not _v2_present(effective):
        return None
    from ..migration.steps_v1 import build_capability_step

    target = _mapping_target(build_capability_step(data_home=effective).mappings, "pane-tools")
    data = _read_data("pane-tools", target)
    return data if isinstance(data, dict) else None


def read_skill_policy(*, data_home: Path | None = None) -> dict | None:
    effective = _effective_data_home(data_home)
    if not _v2_present(effective):
        return None
    from ..migration.steps_v1 import build_capability_step

    target = _mapping_target(build_capability_step(data_home=effective).mappings, "skill-policy")
    data = _read_data("skill-policy", target)
    return data if isinstance(data, dict) else None


def read_routing(*, data_home: Path | None = None) -> dict | None:
    """``{"global": {...}, "projects": {name: {...}}}`` — mirrors
    ``dual_write_routing``'s payload shape verbatim (no ``.data`` wrapper)."""
    effective = _effective_data_home(data_home)
    if not _v2_present(effective):
        return None
    from ..migration.steps_v1 import RoleAgentMigrationStep

    target = RoleAgentMigrationStep(data_home=effective)._routing_target()
    raw = _read_raw("role-providers", target)
    if not isinstance(raw, dict):
        return None
    global_data = raw.get("global")
    projects = raw.get("projects")
    if not isinstance(global_data, dict) or not isinstance(projects, dict):
        _log.warning(
            "v2_read_failed name=%r target=%r err=unexpected-shape", "role-providers", target
        )
        return None
    return {"global": global_data, "projects": projects}


def read_custom_roles_registry(*, data_home: Path | None = None) -> dict | None:
    effective = _effective_data_home(data_home)
    if not _v2_present(effective):
        return None
    from ..migration.steps_v1 import RoleAgentMigrationStep

    target = RoleAgentMigrationStep(data_home=effective)._custom_roles_target()
    data = _read_data("custom-roles", target)
    return data if isinstance(data, dict) else None


def read_projects_registry(*, data_home: Path | None = None) -> dict | None:
    effective = _effective_data_home(data_home)
    if not _v2_present(effective):
        return None
    from ..migration.steps_v1 import ProjectMigrationStep

    target = ProjectMigrationStep(data_home=effective)._registry_target()
    data = _read_data("projects-registry", target)
    return data if isinstance(data, dict) else None


def read_local_issues(source_path: Path, *, data_home: Path | None = None) -> list | None:
    """Only ever non-``None`` for the cockpit-bug global source
    (``DATA_HOME/.takkub_issues.json``) — a per-project ``.takkub_issues.json``
    has no V2 mapping (mirrors ``dual_write_local_issues``'s own source-path
    gate), so any other cwd's call always falls back to V1."""
    effective = _effective_data_home(data_home)
    if not _v2_present(effective):
        return None
    from ..migration.steps_v1 import build_state_step

    mapping = next(
        (m for m in build_state_step(data_home=effective).mappings if m.name == "local-issues"),
        None,
    )
    if mapping is None:
        return None
    try:
        if source_path.resolve() != mapping.source.resolve():
            return None
    except OSError:
        return None
    data = _read_data("local-issues", mapping.target)
    return data if isinstance(data, list) else None


def read_issue_dedup(*, data_home: Path | None = None) -> dict | None:
    effective = _effective_data_home(data_home)
    if not _v2_present(effective):
        return None
    from ..migration.steps_v1 import build_state_step

    target = _mapping_target(build_state_step(data_home=effective).mappings, "issue-dedup")
    data = _read_data("issue-dedup", target)
    return data if isinstance(data, dict) else None


def read_remote_sessions(*, data_home: Path | None = None) -> dict | None:
    effective = _effective_data_home(data_home)
    if not _v2_present(effective):
        return None
    from ..migration.steps_v1 import build_state_step

    target = _mapping_target(build_state_step(data_home=effective).mappings, "remote-sessions")
    data = _read_data("remote-sessions", target)
    return data if isinstance(data, dict) else None


def authority_state(data_home: Path | None = None) -> str:
    """``"v2"`` when the flag is ON and a V2 layout exists to read from,
    else ``"mixed"`` (the existing `layout_state()` ceiling — see that
    function's own docstring for why `"v2"` is otherwise unreachable: the
    ladder is copy-never-move, so V1 files are always still on disk).
    Consulted by `doctor --storage-layout` (plan §3 Wave E item 3), kept
    separate from `layout_state()` itself so that function's
    existence-only contract (used elsewhere, e.g. the auto-migrate boot
    gate) never changes shape because of this flag."""
    from .layout import layout_state

    state = layout_state(data_home)
    if state != "v1" and v2_authority_enabled():
        return "v2"
    return state
