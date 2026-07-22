"""Create/Update/Delete command DTOs for the Roles vertical slice.

Each Update command carries the FULL desired state (not a partial patch) —
the detail pane always loads a complete draft from ``get()`` and edits it in
place, so ``update()`` never has to guess which fields the caller meant to
leave alone. Tri-state MCP/Plugin fields use ``None`` for "use role
defaults" per ``models.USE_DEFAULTS``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoleAccessDraft:
    provider: str
    skills: list[str]
    mcps: list[str] | None
    plugins: list[str] | None


@dataclass(frozen=True)
class RoleGeneralDraft:
    label: str
    color: str
    column: int
    row: int
    instructions: str


@dataclass(frozen=True)
class CreateRoleCommand:
    name: str
    general: RoleGeneralDraft
    access: RoleAccessDraft


@dataclass(frozen=True)
class UpdateRoleCommand:
    general: RoleGeneralDraft
    access: RoleAccessDraft


@dataclass(frozen=True)
class DeleteRoleCommand:
    confirmed_plan_version: str


@dataclass(frozen=True)
class CreateSkillCommand:
    name: str
    description: str
    instructions: str


@dataclass(frozen=True)
class UpdateSkillCommand:
    description: str
    instructions: str


@dataclass(frozen=True)
class McpConfigDraft:
    """Editable half of an MCP server config. ``type``/``command``/``args``/
    ``env`` are the fields the New/Edit form exposes; unknown keys the
    upstream config carried (e.g. ``headers``) are preserved by the
    repository, not by this draft — the draft only carries what the form
    can actually edit."""

    command: str
    args: list[str]
    env: dict[str, str]
    type: str = "stdio"


@dataclass(frozen=True)
class CreateMcpCommand:
    name: str
    config: McpConfigDraft


@dataclass(frozen=True)
class UpdateMcpCommand:
    config: McpConfigDraft


@dataclass(frozen=True)
class UpdateProviderCommand:
    """The only editable fields for a BUILT-IN provider are the operational
    enabled/disabled override and the per-provider model override (SPEC.md
    §Providers) — spec definition fields are read-only.

    ``model``: ``None`` = leave untouched, ``""`` = clear back to the CLI's
    own default, any other string = set. Only meaningful when the provider's
    spec has ``model_flag`` set (``ProviderDetail.model_flag_supported``)."""

    enabled: bool
    model: str | None = None


@dataclass(frozen=True)
class CreatePluginCommand:
    """``key`` is the plugin name; ``marketplace`` is optional — leave blank
    to let ``claude plugin install`` resolve it (fails if the key is
    ambiguous across more than one registered marketplace)."""

    key: str
    marketplace: str = ""
