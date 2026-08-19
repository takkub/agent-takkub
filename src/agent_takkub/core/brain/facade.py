"""The Second Brain façade — `TAKKUB_V2_BRAIN` off by default, fail-open
(plan §0 rules 3+4). Nothing wires into this yet (7c Context Builder is the
first real caller); it exists now so 7c has a stable entry point instead of
reaching into `pipeline`/`retrieval` directly.

Flag OFF: `recall()` returns `[]`, `submit()` returns `None` — zero
`MemoryManager`/`RetrievalEngine`/`BrainStore` construction, so a disabled
Second Brain touches no disk. Flag ON: any exception anywhere in the path
falls back to the same empty/`None` result instead of ever raising into a
caller (e.g. a spawn or `done()` path).
"""

from __future__ import annotations

import logging

from agent_takkub.core.models.memory import MemoryRecord, Scope

from .candidate import MemoryCandidate
from .flag import v2_brain_enabled
from .pipeline import MemoryManager, SubmitResult
from .retrieval import RetrievalEngine
from .store import BrainStore

_log = logging.getLogger(__name__)


def recall(
    query: str, *, scope: Scope = Scope.PROJECT, project: str | None = None, limit: int = 10
) -> list[MemoryRecord]:
    if not v2_brain_enabled():
        return []
    try:
        engine = RetrievalEngine(BrainStore(project))
        return list(engine.recall(query, scope=scope)[:limit])
    except Exception:
        _log.exception(
            "core.brain.facade.recall failed query=%r scope=%r project=%r (fail-open)",
            query,
            scope,
            project,
        )
        return []


def submit(candidate: MemoryCandidate, *, project: str | None = None) -> SubmitResult | None:
    if not v2_brain_enabled():
        return None
    try:
        manager = MemoryManager(BrainStore(project))
        return manager.submit_candidate(candidate)
    except Exception:
        _log.exception(
            "core.brain.facade.submit failed source=%r project=%r (fail-open)",
            candidate.source,
            project,
        )
        return None


__all__ = ["recall", "submit"]
