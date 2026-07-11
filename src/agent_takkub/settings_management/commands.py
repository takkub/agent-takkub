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
