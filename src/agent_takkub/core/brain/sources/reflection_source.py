"""Heuristic ingest for a pane's own `done()`/`subagent_done()` note — the
Reflection hook (epic #309, Phase 7c). Not a classifier: takes the note's
first non-empty line (the same "first line is the headline" heuristic
`orchestrator._condense_done_note`/`core.conversation.summary._headline`
already use) and files it as a FEEDBACK candidate on a failed report
(something to avoid next time) or a PROJECT candidate on a clean one (a
fact about what changed) — trust=AGENT_REPORTED, never COCKPIT_MEASURED
(that split is `digest_facts_source.py`'s job, not this module's).
"""

from __future__ import annotations

from agent_takkub.core.models.memory import CandidateConfidence, MemoryKind, Scope, Trust

from ..candidate import MemoryCandidate

_MIN_CHARS = 8


def _headline(note: str) -> str:
    stripped = (note or "").strip()
    return stripped.splitlines()[0].strip() if stripped else ""


def from_done_note(
    note: str,
    *,
    project: str | None,
    role: str,
    failed: bool,
    task_id: str | None = None,
) -> MemoryCandidate | None:
    headline = _headline(note)
    if len(headline) < _MIN_CHARS:
        return None
    return MemoryCandidate(
        kind=MemoryKind.FEEDBACK if failed else MemoryKind.PROJECT,
        content=headline,
        scope=Scope.AGENT,
        trust=Trust.AGENT_REPORTED,
        confidence=CandidateConfidence.INFERRED,
        source="reflection_note",
        project_id=project,
        agent_id=role,
        task_id=task_id,
    )


__all__ = ["from_done_note"]
