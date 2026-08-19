"""Pure scheduling vocabulary (epic #309 Phase 8a,
docs/v2/07_SCHEDULER_RESOURCE_RUNTIME.md §4-9). Nothing here talks to
psutil/PyQt6/orchestrator — `resource_governor.py` (EXTEND,
REUSE_VS_REWRITE_MATRIX.md §5) is the one caller that turns these into real
admission decisions via `.facade`."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum


class Priority(IntEnum):
    """Lower value = scheduled first. IntEnum (not StrEnum) so ordering is
    the enum's own identity — no separate rank table to keep in sync."""

    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3
    BACKGROUND = 4


class BackpressureLevel(StrEnum):
    NORMAL = "normal"
    THROTTLE_NEW = "throttle_new"
    PAUSE_BACKGROUND = "pause_background"
    QUEUE = "queue"


@dataclass(frozen=True, slots=True)
class SlotPolicy:
    """Every dimension defaults to unlimited/absent — an all-defaults
    `SlotPolicy()` denies nothing, which is what keeps `resource_governor`
    byte-identical to its pre-Phase-8a behavior when a caller doesn't build
    one explicitly (§4-7: global/provider/account/project maxAgents/maxPanes/
    maxConcurrent)."""

    max_agents_global: int | None = None
    max_panes_global: int | None = None
    provider_max_concurrent: Mapping[str, int] = field(default_factory=dict)
    account_max_concurrent: Mapping[str, int] = field(default_factory=dict)
    project_max_agents: Mapping[str, int] = field(default_factory=dict)
    project_max_panes: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SlotRequest:
    project_id: str
    pane_id: str
    task_id: str
    provider_id: str | None = None
    account_id: str | None = None
    priority: Priority = Priority.NORMAL


@dataclass(frozen=True, slots=True)
class ActiveCounts:
    """Current usage, one integer per dimension `SlotPolicy` can cap. Built
    by the caller each check (`resource_governor` already tracks tokens) so
    `policy.evaluate` stays a pure function with no dependency on how the
    caller stores its tokens.

    `ponytail:` `agents_global`/`panes_global` are usually fed the same
    count by `resource_governor` (one token == one active pane == one active
    agent there today) — kept as two fields because §07 names them
    separately and a future caller may track them apart; upgrade path is
    just passing different numbers in, no shape change needed.
    """

    agents_global: int = 0
    panes_global: int = 0
    by_provider: Mapping[str, int] = field(default_factory=dict)
    by_account: Mapping[str, int] = field(default_factory=dict)
    by_project_agents: Mapping[str, int] = field(default_factory=dict)
    by_project_panes: Mapping[str, int] = field(default_factory=dict)
