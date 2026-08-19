"""MemoryManager — the candidate pipeline (plan §9-20): validate → dedup
(normalized text + a token-set "semantic-ish" key — no embeddings/vector DB,
the blueprint forbids vector-only) → scope (candidate already carries it,
the manager just persists it) → confidence (`CandidateConfidence` →
`Confidence`, `core.models.memory.confidence_for`) → conflict detect +
supersede (never deletes: the OLD record flips to `SUPERSEDED` with
`superseded_by` pointing at the NEW record's id, per plan "ไม่ลบ history")
→ version → persist via `BrainStore`.

Dedup/conflict search is scoped to the same (kind, scope, project_id,
agent_id) bucket within the store — bounded by one project's record count,
never a global O(n²) scan.
"""

from __future__ import annotations

import dataclasses
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum

from agent_takkub.bm25_search import tokenize
from agent_takkub.core.models.memory import MemoryRecord, RecordStatus, confidence_for

from .candidate import MemoryCandidate
from .store import BrainStore

# Below this token-set Jaccard similarity, two candidates are treated as
# unrelated facts (both persisted independently) rather than a conflicting
# restatement of the same fact.
_SEMANTIC_MATCH_THRESHOLD = 0.6

_MIN_CONTENT_CHARS = 3


class SubmitStatus(StrEnum):
    CREATED = "created"
    DUPLICATE = "duplicate"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class SubmitResult:
    status: SubmitStatus
    record: MemoryRecord | None
    reason: str = ""


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split()).rstrip(".·!?,;: ")


def _token_set(text: str) -> frozenset[str]:
    return frozenset(tokenize(text))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _same_bucket(candidate: MemoryCandidate, record: MemoryRecord) -> bool:
    return (
        record.kind == candidate.kind
        and record.scope == candidate.scope
        and record.project_id == candidate.project_id
        and record.agent_id == candidate.agent_id
    )


class MemoryManager:
    def __init__(self, store: BrainStore) -> None:
        self._store = store

    def _validate(self, candidate: MemoryCandidate) -> str | None:
        """Returns a rejection reason, or None if valid."""
        content = (candidate.content or "").strip()
        if len(content) < _MIN_CONTENT_CHARS:
            return "content too short"
        return None

    def _find_match(
        self, candidate: MemoryCandidate, active: list[MemoryRecord]
    ) -> tuple[MemoryRecord | None, bool]:
        """Returns (matched_record, is_exact). `is_exact` means the
        normalized text is identical (true duplicate, no-op); otherwise a
        match means "same subject, different wording" (supersede)."""
        norm = _normalize(candidate.content)
        tokens = _token_set(candidate.content)
        bucket = [r for r in active if _same_bucket(candidate, r)]
        for record in bucket:
            if _normalize(record.content) == norm:
                return record, True
        best: MemoryRecord | None = None
        best_score = 0.0
        for record in bucket:
            score = _jaccard(tokens, _token_set(record.content))
            if score > best_score:
                best, best_score = record, score
        if best is not None and best_score >= _SEMANTIC_MATCH_THRESHOLD:
            return best, False
        return None, False

    def _build_record(self, candidate: MemoryCandidate, *, version: int) -> MemoryRecord:
        return MemoryRecord(
            id=uuid.uuid4().hex,
            kind=candidate.kind,
            content=candidate.content.strip(),
            scope=candidate.scope,
            trust=candidate.trust,
            confidence=confidence_for(candidate.confidence),
            created_at=candidate.created_at,
            user_id=candidate.user_id,
            workspace_id=candidate.workspace_id,
            project_id=candidate.project_id,
            agent_id=candidate.agent_id,
            task_id=candidate.task_id,
            session_id=candidate.session_id,
            source=candidate.source,
            version=version,
            status=RecordStatus.ACTIVE,
        )

    def submit_candidate(self, candidate: MemoryCandidate) -> SubmitResult:
        """The ONLY write path into the Second Brain (plan: "agent ห้ามเขียน
        brain ตรง" — the only API is submit_candidate)."""
        reason = self._validate(candidate)
        if reason is not None:
            return SubmitResult(SubmitStatus.REJECTED, None, reason)

        active = self._store.load_active()
        matched, is_exact = self._find_match(candidate, active)

        if matched is not None and is_exact:
            return SubmitResult(SubmitStatus.DUPLICATE, matched)

        version = (matched.version + 1) if matched is not None else 1
        new_record = self._build_record(candidate, version=version)

        if matched is not None:
            superseded = dataclasses.replace(
                matched,
                status=RecordStatus.SUPERSEDED,
                superseded_by=new_record.id,
                updated_at=time.time(),
            )
            self._store.append_event(superseded)
            self._store.append_event(new_record)
            return SubmitResult(SubmitStatus.SUPERSEDED, new_record)

        self._store.append_event(new_record)
        return SubmitResult(SubmitStatus.CREATED, new_record)


__all__ = ["MemoryManager", "SubmitResult", "SubmitStatus"]
