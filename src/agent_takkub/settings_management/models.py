"""Shared DTOs for the settings-management module — no Qt, no I/O.

Pure dataclasses/enums only, per the repository contract in
``docs/design/2026-07-11-settings-redesign-codex.md`` §Repository contract.
Every entity repository (roles now; skills/mcps/plugins/providers in later
phases) returns/accepts these shapes so ``pages/`` never has to know a JSON
file exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class EntityKind(StrEnum):
    ROLE = "role"
    SKILL = "skill"
    MCP = "mcp"
    PLUGIN = "plugin"
    PROVIDER = "provider"


class Ownership(StrEnum):
    """Who "owns" an entity — drives the source badge + read-only affordance."""

    BUILT_IN = "built-in"
    CUSTOM = "custom"
    MANAGED = "managed"
    EXTERNAL = "external"
    PROJECT = "project"
    SHIPPED = "shipped"
    USER = "user"


# Tri-state sentinel for MCP/Plugin access — must stay distinguishable from
# an explicit empty list (SPEC.md "Defaults vs empty"). ``None`` on the wire
# means "use role defaults / no policy entry"; ``[]`` means "explicit empty
# allowlist"; a non-empty list is an explicit selection.
USE_DEFAULTS = None


@dataclass(frozen=True)
class Capability:
    """What operations are actually available for one entity (or the
    entity kind as a whole, when ``entity_id`` is None) right now.

    A repository must never let the UI show a button whose action isn't
    actually possible — see SPEC.md "กติกาเหล็ก". ``reason`` is populated
    whenever any of the three booleans is False, so the UI has copy to show
    next to a disabled/hidden control instead of a bare `False`.
    """

    can_create: bool = True
    can_update: bool = True
    can_delete: bool = True
    reason: str = ""


@dataclass(frozen=True)
class OperationResult:
    """Outcome of a create/update/delete call."""

    ok: bool
    message: str = ""
    entity_id: str | None = None


@dataclass(frozen=True)
class DeletePlan:
    """What would happen if ``delete(entity_id, version)`` were called now.

    ``version`` is an opaque token the caller must echo back to ``delete()``;
    it changes whenever the underlying state this plan was computed from
    changes, so a stale plan (computed before some other write raced in)
    is rejected instead of silently deleting the wrong thing.
    """

    entity_id: str
    deletable: bool
    version: str
    effects: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoleAccess:
    """A role's Access-tab state: provider + skills + MCP/plugin tri-state."""

    provider: str
    provider_forced: bool
    provider_available: bool
    skills: tuple[str, ...] = ()
    mcps: tuple[str, ...] | None = USE_DEFAULTS
    plugins: tuple[str, ...] | None = USE_DEFAULTS


@dataclass(frozen=True)
class RoleSummary:
    """One row in the Roles list."""

    name: str
    label: str
    color: str
    ownership: Ownership
    column: int
    row: int


@dataclass(frozen=True)
class RoleDetail:
    """Everything the Role detail pane (General + Access + Advanced) needs."""

    name: str
    label: str
    color: str
    ownership: Ownership
    column: int
    row: int
    instructions: str
    instructions_path: str
    access: RoleAccess
    capabilities: Capability = field(default_factory=Capability)


@dataclass(frozen=True)
class SkillSummary:
    """One row in the Skills list."""

    name: str
    description: str
    ownership: Ownership


@dataclass(frozen=True)
class SkillDetail:
    """Everything the Skill detail pane (General + Assigned roles) needs."""

    name: str
    description: str
    instructions: str
    path: str
    ownership: Ownership
    assigned_roles: tuple[str, ...] = ()
    capabilities: Capability = field(default_factory=Capability)


@dataclass(frozen=True)
class PluginSummary:
    """One row in the Plugins list. ``id`` is the ``claude plugin`` identity
    (``<key>@<marketplace>``); ``key``/``marketplace`` are that id split for
    display + governance lookups. ``blocked`` flags a plugin identity the
    cockpit never injects into a teammate pane even when its marketplace is
    otherwise allowed for a role (``lead_context._PANE_PLUGIN_DENYLIST``)."""

    id: str
    key: str
    marketplace: str
    version: str
    enabled: bool
    ownership: Ownership
    blocked: bool = False
    blocked_reason: str = ""


@dataclass(frozen=True)
class PluginDetail:
    """Everything the Plugin detail pane (General + Allowed roles) needs.

    Nothing here is form-editable — identity/version are external
    marketplace metadata (SPEC.md "edit = assignment เท่านั้น"), and even
    assignment is read-only on THIS page: it's governed per-marketplace by
    ``pane_tools_policy`` and edited from the Role's Access tab, same
    relationship as ``McpDetail.allowed_roles``. ``governable`` is False when
    the plugin's marketplace isn't one the cockpit can push into panes at all
    (``pane_tools_dialog.discover_marketplaces``) — ``allowed_roles`` is then
    always empty because there's no policy to read.
    """

    id: str
    key: str
    marketplace: str
    version: str
    enabled: bool
    scope: str
    install_path: str
    installed_at: str
    ownership: Ownership
    blocked: bool
    blocked_reason: str
    governable: bool
    allowed_roles: tuple[str, ...] = ()
    capabilities: Capability = field(default_factory=Capability)


@dataclass(frozen=True)
class McpSummary:
    """One row in the MCP Servers list."""

    name: str
    command: str
    ownership: Ownership


@dataclass(frozen=True)
class McpDetail:
    """Everything the MCP Server detail pane (General + Allowed roles +
    Diagnostics) needs. ``config`` is already secret-masked — repositories
    must never hand the UI an unmasked credential (SPEC.md "MCP Servers")."""

    name: str
    config: dict
    ownership: Ownership
    has_secrets: bool
    allowed_roles: tuple[str, ...] = ()
    capabilities: Capability = field(default_factory=Capability)
