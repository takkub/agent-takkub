"""Contract for the Second Brain (Phase 6 — candidate pipeline, retrieval,
Context Builder at assignment-time, reflection at done). Not in the plan's
§3.2 file list, but requested explicitly for this spawn — kept minimal
since Phase 6 owns the real design."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from agent_takkub.core.models.memory import MemoryRecord, Scope


@runtime_checkable
class BrainAdapter(Protocol):
    def recall(self, query: str, *, scope: Scope, limit: int = 10) -> Sequence[MemoryRecord]: ...

    def remember(self, record: MemoryRecord) -> None: ...
