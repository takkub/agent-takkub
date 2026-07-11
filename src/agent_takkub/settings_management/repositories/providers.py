"""Provider repository — adapter over :mod:`~...provider_spec` (frozen
BUILT-IN registry, read-only) + :mod:`~...provider_config` (per-role
assignment) + :mod:`~...provider_state` (enable/disable toggle).

UI (``pages/providers_page.py``) only ever talks to this module; it never
imports ``provider_spec``/``provider_config``/``provider_state`` directly
(SPEC.md "UI ห้าม import JSON path ตรง").

Two layers per SPEC.md §Providers:
  - **Spec definition** (binary/flags/ready rules/capabilities): BUILT-IN,
    read-only, sourced straight from ``PROVIDER_REGISTRY``. No create/update/
    delete on this layer in this phase (custom provider spec CRUD stays
    hidden behind a capability flag until a registry service lands — SPEC.md
    Phase 4).
  - **Operational override** (enabled/disabled + role assignment): the only
    writable field is ``enabled``, via :func:`update`. Role assignment is
    read-only here too — it's edited from the Role's Access tab (SPEC.md
    "Relationships").
"""

from __future__ import annotations

from ... import provider_config, provider_state
from ... import roles as roles_mod
from ...provider_spec import PROVIDER_REGISTRY, ProviderSpec
from ..commands import UpdateProviderCommand
from ..models import (
    Capability,
    OperationResult,
    Ownership,
    ProviderCapabilities,
    ProviderDetail,
    ProviderSummary,
)


class ProviderNotFoundError(KeyError):
    pass


def _required(name: str) -> bool:
    """True for providers whose CLI is fixed cockpit infrastructure and can
    never be toggled off (currently just ``claude`` — see
    ``provider_state.TOGGLABLE``)."""
    return name not in provider_state.TOGGLABLE


def _installed(spec: ProviderSpec) -> tuple[bool, str]:
    if spec.custom_discovery_fn is None:
        return True, ""
    try:
        path = spec.custom_discovery_fn()
    except Exception:
        return False, ""
    return path is not None, path or ""


def _enabled(name: str) -> bool:
    return True if _required(name) else not provider_state.is_disabled(name)


def _assigned_roles(name: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            role
            for role in roles_mod.all_role_names()
            if provider_config.provider_for(role) == name
        )
    )


def list(query: str = "") -> list[ProviderSummary]:  # contract name (models.py Repository contract)
    query = (query or "").strip().lower()
    out: list[ProviderSummary] = []
    for name, spec in PROVIDER_REGISTRY.items():
        if query and query not in name.lower():
            continue
        installed, _ = _installed(spec)
        out.append(
            ProviderSummary(
                name=name,
                label=spec.name.capitalize(),
                ownership=Ownership.BUILT_IN,
                installed=installed,
                enabled=_enabled(name),
                required=_required(name),
            )
        )
    return out


def get(entity_id: str) -> ProviderDetail:
    spec = PROVIDER_REGISTRY.get(entity_id)
    if spec is None:
        raise ProviderNotFoundError(entity_id)
    installed, path = _installed(spec)
    return ProviderDetail(
        name=entity_id,
        label=spec.name.capitalize(),
        ownership=Ownership.BUILT_IN,
        binary_names=tuple(spec.binary_names),
        install_instructions=spec.install_instructions,
        installed=installed,
        binary_path=path,
        enabled=_enabled(entity_id),
        required=_required(entity_id),
        spec_capabilities=ProviderCapabilities(
            context_strategy=spec.context_strategy,
            supports_mirror=spec.supports_mirror,
            supports_resume=spec.supports_resume,
            supports_slash_commands=spec.supports_slash_commands,
            supports_hooks=spec.supports_hooks,
            supports_browser_profiles=spec.supports_browser_profiles,
        ),
        assigned_roles=_assigned_roles(entity_id),
        capabilities=capabilities(entity_id),
    )


def capabilities(entity_id: str | None = None) -> Capability:
    if entity_id is None:
        return Capability(can_create=False, can_delete=False)
    if entity_id not in PROVIDER_REGISTRY:
        return Capability(
            can_create=False, can_update=False, can_delete=False, reason="ไม่พบ provider นี้"
        )
    if _required(entity_id):
        return Capability(
            can_create=False,
            can_update=False,
            can_delete=False,
            reason="Provider นี้เป็น cockpit infrastructure ที่บังคับใช้ — ปิดใช้งานไม่ได้",
        )
    return Capability(can_create=False, can_update=True, can_delete=False)


def update(entity_id: str, command: UpdateProviderCommand) -> OperationResult:
    """Persist the operational enabled/disabled override. Spec-definition
    fields aren't writable in this phase — there's nothing else to update."""
    if entity_id not in PROVIDER_REGISTRY:
        return OperationResult(ok=False, message="ไม่พบ provider นี้", entity_id=entity_id)
    if _required(entity_id):
        return OperationResult(ok=False, message="Provider นี้ปิดใช้งานไม่ได้", entity_id=entity_id)
    try:
        provider_state.set_disabled(entity_id, not command.enabled)
    except ValueError as e:
        return OperationResult(ok=False, message=str(e), entity_id=entity_id)
    return OperationResult(ok=True, entity_id=entity_id)
