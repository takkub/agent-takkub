"""Memory vocabulary (Phase 7a/7b — Second Brain: candidate pipeline,
retrieval; Context Builder is 7c). `Trust` is the 5-level scale the plan
(§3.4) says to extend from `digest_facts.py`'s "cockpit measured vs.
agent-typed" distinction — never conflate a self-reported claim with a
cockpit-verified fact. `MemoryKind` mirrors this project's own auto-memory
taxonomy (user/feedback/project/reference, ~/.claude-work memory system).

`CandidateConfidence` is a SEPARATE, finer scale from `Confidence`: it's
the pipeline's internal scoring input (`core/brain/candidate.py`'s
`MemoryCandidate`, patterned after `digest_facts.py`'s measured-vs-reported
split) and maps DOWN onto the coarser, already-tested `Confidence` a
persisted `MemoryRecord` carries (`CONFIDENCE_WEIGHT`/`confidence_for`
below) — the two are never the same enum so extending pipeline scoring can
never touch `Confidence`'s existing 3-level contract (test_core_models.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Trust(StrEnum):
    COCKPIT_MEASURED = "cockpit_measured"
    USER_CONFIRMED = "user_confirmed"
    LEAD_CONFIRMED = "lead_confirmed"
    AGENT_REPORTED = "agent_reported"
    EXTERNAL_UNTRUSTED = "external_untrusted"


class MemoryKind(StrEnum):
    USER = "user"
    FEEDBACK = "feedback"
    PROJECT = "project"
    REFERENCE = "reference"
    DECISION = "decision"


class Scope(StrEnum):
    GLOBAL = "global"
    WORKSPACE = "workspace"
    PROJECT = "project"
    AGENT = "agent"
    # Added Phase 7a (candidate pipeline needs finer-grained scoping than
    # the original 4): a user-level preference, a single task's working
    # memory, and a single pane session's scratch memory.
    USER = "user"
    TASK = "task"
    SESSION = "session"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CandidateConfidence(StrEnum):
    """Pipeline-internal confidence a `MemoryCandidate` arrives with —
    finer than the persisted `Confidence` scale, modeled on digest_facts.py's
    measured-vs-reported provenance split. `CONFIDENCE_WEIGHT` gives each
    level a numeric weight for retrieval ranking; `confidence_for` maps a
    level down onto `Confidence` at persist time."""

    CONFIRMED = "confirmed"
    HIGH = "high"
    INFERRED = "inferred"
    UNVERIFIED = "unverified"


CONFIDENCE_WEIGHT: dict[CandidateConfidence, float] = {
    CandidateConfidence.CONFIRMED: 1.0,
    CandidateConfidence.HIGH: 0.85,
    CandidateConfidence.INFERRED: 0.6,
    CandidateConfidence.UNVERIFIED: 0.4,
}

_CONFIDENCE_DOWNMAP: dict[CandidateConfidence, Confidence] = {
    CandidateConfidence.CONFIRMED: Confidence.HIGH,
    CandidateConfidence.HIGH: Confidence.HIGH,
    CandidateConfidence.INFERRED: Confidence.MEDIUM,
    CandidateConfidence.UNVERIFIED: Confidence.LOW,
}


def confidence_for(candidate_confidence: CandidateConfidence) -> Confidence:
    return _CONFIDENCE_DOWNMAP[candidate_confidence]


class RecordStatus(StrEnum):
    """A superseded record is never deleted (plan §9-20: "ไม่ลบ history") —
    it stays in the log with `status=SUPERSEDED` and `superseded_by` pointing
    at the record that replaced it."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    id: str
    kind: MemoryKind
    content: str
    scope: Scope = Scope.PROJECT
    trust: Trust = Trust.AGENT_REPORTED
    confidence: Confidence = Confidence.MEDIUM
    created_at: float | None = None
    user_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None
    # Added Phase 7a, all backward-compatible (defaulted, appended after the
    # original field set so every existing positional/keyword construction
    # site keeps working unchanged).
    agent_id: str | None = None
    task_id: str | None = None
    session_id: str | None = None
    source: str = "unknown"
    version: int = 1
    status: RecordStatus = RecordStatus.ACTIVE
    superseded_by: str | None = None
    updated_at: float | None = None
