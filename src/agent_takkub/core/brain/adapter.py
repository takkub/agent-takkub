"""NativeBrainAdapter — implements `core.contracts.brain_adapter.BrainAdapter`
over `MemoryManager` + `RetrievalEngine`. `remember()` is the Protocol's
required write method, but it never writes a record directly: it converts
the given `MemoryRecord` into a `MemoryCandidate` and routes it through
`MemoryManager.submit_candidate` — the pipeline's only write path (plan:
"agent ห้ามเขียน brain ตรง (API มีแต่ submit_candidate)").
"""

from __future__ import annotations

from collections.abc import Sequence

from agent_takkub.core.models.memory import CandidateConfidence, MemoryRecord, Scope

from .candidate import MemoryCandidate
from .pipeline import MemoryManager
from .retrieval import RetrievalEngine

# `MemoryRecord.confidence` is the coarser, persisted 3-level scale;
# `MemoryCandidate.confidence` wants the finer pipeline scale. Re-entering
# an already-persisted record through the pipeline maps it back up to the
# nearest candidate level rather than always defaulting to INFERRED, so a
# HIGH-confidence record doesn't get silently downgraded going through
# `remember()` again (e.g. a caller re-submitting a record it read back).
_CONFIDENCE_UPMAP = {
    "low": CandidateConfidence.UNVERIFIED,
    "medium": CandidateConfidence.INFERRED,
    "high": CandidateConfidence.HIGH,
}


class NativeBrainAdapter:
    def __init__(self, manager: MemoryManager, retrieval: RetrievalEngine) -> None:
        self._manager = manager
        self._retrieval = retrieval

    def recall(self, query: str, *, scope: Scope, limit: int = 10) -> Sequence[MemoryRecord]:
        return self._retrieval.recall(query, scope=scope)[:limit]

    def remember(self, record: MemoryRecord) -> None:
        candidate = MemoryCandidate(
            kind=record.kind,
            content=record.content,
            scope=record.scope,
            trust=record.trust,
            confidence=_CONFIDENCE_UPMAP.get(record.confidence.value, CandidateConfidence.INFERRED),
            source=record.source,
            user_id=record.user_id,
            workspace_id=record.workspace_id,
            project_id=record.project_id,
            agent_id=record.agent_id,
            task_id=record.task_id,
            session_id=record.session_id,
        )
        self._manager.submit_candidate(candidate)


__all__ = ["NativeBrainAdapter"]
