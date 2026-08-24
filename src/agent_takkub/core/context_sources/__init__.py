"""Context Sources (issue #372, `15_CONTEXT_SOURCE_ARCHITECTURE.md`) —
`ContextSource` implementations `core.brain.context_builder`'s merge step
draws from: `brain_source`, `conversation_source`, `resource_source`
(curated Obsidian docs), `openviking_source` (optional sidecar). Graft
stays tool-driven per `13_GRAFT_FINAL_ROLE.md` and has no source here.
"""

from __future__ import annotations

from .base import ContextItem, ContextSource

__all__ = ["ContextItem", "ContextSource"]
