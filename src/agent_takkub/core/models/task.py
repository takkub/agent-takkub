"""Task vocabulary. `TaskState` is deliberately its own enum, not borrowed
from `task_ledger.py`'s display symbols ("working"/"ok"/"fail"/"closed"/
"superseded") — that module renders ledger rows, this one is the V2 lifecycle
a future `AgentAdapter`/scheduler drives.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TaskState(StrEnum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    RUNNING = "running"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    title: str
    description: str = ""
    state: TaskState = TaskState.PENDING
    assignee_agent_id: str | None = None
    parent_task_id: str | None = None
    user_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None
