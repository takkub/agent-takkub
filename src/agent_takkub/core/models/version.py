"""Version/compatibility vocabulary (Phase 3 target — version.json, compat
matrix, migration engine). NEW.
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
    note: str = ""
    user_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None
