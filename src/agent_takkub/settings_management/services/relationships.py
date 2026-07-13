"""Role's Access tab — read/write provider + skills + MCP/plugin allowlists.

One role is the aggregate root for all four relationship stores
(`provider_config`, `skill_policy`, `pane_tools_policy` x2). Reads compose
the four `effective_*` lookups into one :class:`models.RoleAccess`; writes
wrap all four stores (plus MCP role-variant regen, HIGH-4) in one
:class:`transaction.FileTransaction` so a failed write to any store rolls
the others back (SPEC.md "failed multi-file write rollback").

**MCP/Plugin tri-state limitation (inherited from `pane_tools_policy`, not
introduced here):** the on-disk schema tracks "has an override entry" per
ROLE, not independently per kind. Reverting only ONE of mcps/plugins to
"use defaults" while the other stays an explicit override is not
representable — the legacy Matrix UI has this same limitation.
``write_access`` mirrors that faithfully: pass ``None`` for both to fully
reset, or a list for whichever kind(s) you're setting explicitly.
"""

from __future__ import annotations

from ... import pane_tools_policy, provider_config, shared_dev_tools, skill_policy
from .. import models
from ..commands import RoleAccessDraft
from ..models import OperationResult
from ..transaction import FileTransaction


def _provider_available(role: str, desired: str) -> bool:
    if desired == provider_config.CLAUDE:
        return True
    return provider_config.effective_provider_for(role) == desired


def get_role_access(name: str) -> models.RoleAccess:
    provider = provider_config.provider_for(name)
    mcps = pane_tools_policy.effective_mcps(name)
    plugins = pane_tools_policy.effective_plugins(name)
    return models.RoleAccess(
        provider=provider,
        provider_forced=name in provider_config.FORCED_ROLES,
        provider_available=_provider_available(name, provider),
        skills=tuple(skill_policy.effective_skills(name)),
        mcps=tuple(sorted(mcps)) if mcps is not None else None,
        plugins=tuple(sorted(plugins)) if plugins is not None else None,
    )


def _relationship_paths(*role_names: str) -> list:
    return [
        provider_config.config_path(None),
        pane_tools_policy.PANE_TOOLS_POLICY_FILE,
        skill_policy.SKILL_POLICY_FILE,
        *shared_dev_tools.role_variant_paths(role_names),
    ]


def _apply_access(name: str, draft: RoleAccessDraft) -> None:
    """Raw store writes for one role's Access-tab draft. Raises
    ``RuntimeError``/``OSError`` on failure — no transaction of its own, so
    a caller that needs to combine this with OTHER stores' writes in one
    atomic unit (role create/update, HIGH-2) can wrap a single
    `FileTransaction` around both instead of nesting two independent ones.
    """
    provider_config.save_role_overrides({name: draft.provider}, scope=[name])

    if not skill_policy.set_role_skills(name, list(draft.skills)):
        raise RuntimeError("เขียน skill policy ไม่สำเร็จ")

    had_tools_override = name in pane_tools_policy.load_policy()
    mcps_changed = False
    plugins_changed = False
    if draft.mcps is None and draft.plugins is None:
        if not pane_tools_policy.reset_role(name):
            raise RuntimeError("เขียน MCP/plugin policy ไม่สำเร็จ")
        mcps_changed = True
        plugins_changed = True
    else:
        if draft.mcps is not None:
            if not pane_tools_policy.set_role_items(name, "mcps", list(draft.mcps)):
                raise RuntimeError("เขียน MCP policy ไม่สำเร็จ")
            mcps_changed = True
        if draft.plugins is not None:
            if not pane_tools_policy.set_role_items(name, "plugins", list(draft.plugins)):
                raise RuntimeError("เขียน plugin policy ไม่สำเร็จ")
            plugins_changed = True

        # The schema materialises both kinds together. Setting only plugins on
        # a role that used defaults therefore also makes its MCP default
        # explicit and requires role-variant regeneration.
        if not had_tools_override and plugins_changed:
            mcps_changed = True

    if mcps_changed:
        # HIGH-4: an Access-tab MCP change must regenerate role variants
        # too, or a running/spawned pane keeps consuming a stale variant
        # even though Save reported success (the "no fake button" gap).
        ok, failed = shared_dev_tools.regen_role_variants_checked()
        if not ok:
            raise RuntimeError(f"regenerate MCP role variant ไม่สำเร็จ: {', '.join(failed)}")


def write_access(name: str, draft: RoleAccessDraft) -> OperationResult:
    """Persist a role's Access-tab draft across all stores atomically."""
    try:
        with FileTransaction(_relationship_paths(name)):
            _apply_access(name, draft)
    except (RuntimeError, OSError) as e:
        return OperationResult(ok=False, message=str(e), entity_id=name)

    return OperationResult(ok=True, message="", entity_id=name)
