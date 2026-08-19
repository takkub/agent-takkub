"""Version/compatibility vocabulary — version.json, compat matrix, migration
engine (#309 Phase 4, docs/v2/V2_IMPLEMENTATION_PLAN.md §5).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ComponentVersion:
    id: str
    component: str
    version: str
    released_at: float | None = None
    user_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None


@dataclass(frozen=True, slots=True)
class CompatibilityRule:
    id: str
    component: str
    min_version: str | None = None
    max_version: str | None = None
    # True (default): max_version is an exclusive ceiling (installed must be
    # STRICTLY below it). False: max_version is inclusive. Additive field —
    # every existing construction of this dataclass keeps its old meaning.
    max_exclusive: bool = True
    # Feature flags this component's version range unlocks — checked via
    # CompatibilityMatrix.supports_feature(), not compared numerically.
    features: tuple[str, ...] = ()
    note: str = ""
    user_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None
