"""Agent vocabulary — a role's reusable definition (`AgentTemplate`) vs. one
spawned pane (`AgentInstance`). Distinct from the existing `custom-roles.json`
+ `PaneRegistry` (spawn_engine.py) — those stay REUSE/WRAP in Phase 1
(REUSE_VS_REWRITE_MATRIX.md); nothing here is wired into spawn yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AgentStatus(StrEnum):
    SPAWNING = "spawning"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class AgentTemplate:
    """A role's reusable definition — mirrors `custom-roles.json` shape."""

    id: str
    role: str
    description: str = ""
    default_model_profile_id: str | None = None
    default_account_pool_id: str | None = None
    user_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None


@dataclass(frozen=True, slots=True)
class AgentInstance:
    """One spawned pane, built from an `AgentTemplate`."""

    id: str
    template_id: str
    account_id: str | None = None
    status: AgentStatus = AgentStatus.SPAWNING
    started_at: float | None = None
    user_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None
