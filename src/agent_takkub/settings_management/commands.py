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
