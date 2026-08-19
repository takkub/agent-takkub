"""`MemoryCandidate` — the ONLY shape a fact enters the Second Brain in.
Agents/adapters never construct or write a `MemoryRecord` directly; they
build a candidate and hand it to `MemoryManager.submit_candidate` (or the
`core.brain.facade.submit` fail-open wrapper), which runs it through
validate → dedup → conflict/supersede → persist before anything becomes a
`MemoryRecord` on disk (plan: "agent ห้ามเขียน brain ตรง")."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from agent_takkub.core.models.memory import CandidateConfidence, MemoryKind, Scope, Trust


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    kind: MemoryKind
    content: str
    scope: Scope = Scope.PROJECT
    trust: Trust = Trust.AGENT_REPORTED
    confidence: CandidateConfidence = CandidateConfidence.INFERRED
    source: str = "unknown"
    user_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None
    agent_id: str | None = None
    task_id: str | None = None
    session_id: str | None = None
    created_at: float = field(default_factory=time.time)
