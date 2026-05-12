"""Role registry. Default roles + colors + grid positions.

The cockpit reserves 8 slots in a 3-column grid:

  col 0 (left):   Lead (always-on)
  col 1 (middle): frontend / backend / mobile / devops
  col 2 (right):  designer / qa / reviewer + dynamic add-slot

Custom roles can be added at runtime via Orchestrator.register_role.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Role:
    name: str
    label: str
    color: str  # hex
    column: int  # 0=lead, 1=middle, 2=right
    row: int


LEAD = Role("lead", "Lead", "#f5c542", column=0, row=0)

DEFAULT_TEAMMATES: tuple[Role, ...] = (
    Role("frontend", "Frontend", "#22d3ee", column=1, row=0),
    Role("backend", "Backend", "#3b82f6", column=1, row=1),
    Role("mobile", "Mobile", "#a855f7", column=1, row=2),
    Role("devops", "DevOps", "#22c55e", column=1, row=3),
    Role("designer", "Designer", "#ec4899", column=2, row=0),
    Role("qa", "QA", "#f97316", column=2, row=1),
    Role("reviewer", "Reviewer", "#ef4444", column=2, row=2),
)

ALL_DEFAULT: tuple[Role, ...] = (LEAD, *DEFAULT_TEAMMATES)


def by_name(name: str) -> Role | None:
    name = name.lower().strip()
    for r in ALL_DEFAULT:
        if r.name == name:
            return r
    return None
