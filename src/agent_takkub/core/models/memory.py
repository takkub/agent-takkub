"""Memory vocabulary (Phase 6 target — Second Brain: candidate pipeline,
retrieval, Context Builder). `Trust` is the 5-level scale the plan (§3.4)
says to extend from `digest_facts.py`'s "cockpit measured vs. agent-typed"
distinction — never conflate a self-reported claim with a cockpit-verified
fact. `MemoryKind` mirrors this project's own auto-memory taxonomy
(user/feedback/project/reference, ~/.claude-work memory system).
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


class Scope(StrEnum):
    GLOBAL = "global"
    WORKSPACE = "workspace"
    PROJECT = "project"
    AGENT = "agent"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


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
