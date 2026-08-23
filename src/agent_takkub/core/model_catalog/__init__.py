"""core.model_catalog — model registry store + legacy reader (epic #309,
docs/v2/2.0.0-migration-plan.md §1.3).

Named `model_catalog`, not `core.models.registry`, because the plan's own
`core-models-pure` import-linter contract forbids `agent_takkub.core.models.*`
from importing `agent_takkub.core.storage` at all — see `registry.py`'s
module docstring for the full reasoning.
"""

from __future__ import annotations
