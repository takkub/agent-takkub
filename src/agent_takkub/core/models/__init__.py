"""V2 domain vocabulary — pure dataclasses, stdlib only (docs/v2/V2_IMPLEMENTATION_PLAN.md §3.4).

Every model here is `@dataclass(frozen=True, slots=True)`. Persistent models
carry `user_id`/`workspace_id`/`project_id` (all default `None`) so a
single-user Phase 1 can grow into multi-tenant later without a field
migration — see the plan's R10.
"""

from __future__ import annotations
