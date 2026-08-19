"""Priority-aware reordering for `resource_governor.dispatch_waiting`'s
existing per-project round-robin (docs/v2/07_SCHEDULER_RESOURCE_RUNTIME.md
§8). Deliberately NOT a new queue engine — `resource_governor` keeps its own
`OrderedDict[project] -> deque` and fairness cursor; this module only
decides which project must be tried first when queue-heads at different
priority tiers are both ready, via a stable sort that preserves the
existing round-robin order within a tier."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from .models import Priority


def order_by_priority(
    project_ids: Sequence[str], priority_of: Callable[[str], Priority]
) -> list[str]:
    """Stable-sort `project_ids` (already rotated for round-robin fairness
    by the caller) by the priority of each project's queue-head item.
    Stability keeps ties in the caller's fair rotation order — this only
    ever moves a higher-priority tier earlier, never reorders within one."""
    return sorted(project_ids, key=lambda project_id: int(priority_of(project_id)))
