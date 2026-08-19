"""Ingest adapter for `digest_facts.DigestFacts` — the one source that is
NOT read from a file. `DigestFacts` is built once per `done()` call and
threaded through the digest queue in-memory (never persisted on its own,
see that module's docstring), so this adapter takes the object directly
rather than scanning disk.

trust=COCKPIT_MEASURED: every `DigestFacts` field is git state or
cockpit-owned PaneState (branch/commits/files/merge), the exact split
digest_facts.py pioneered and Trust's 5 levels formalize — never the
agent's own prose (that's `headline`, folded in for context only, same as
`format_digest_fact_line` treats it).
"""

from __future__ import annotations

from agent_takkub.core.models.memory import CandidateConfidence, MemoryKind, Scope, Trust
from agent_takkub.digest_facts import DigestFacts

from ..candidate import MemoryCandidate


def _summarize(facts: DigestFacts) -> str:
    parts = [f"{facts.role} {facts.verdict}"]
    if facts.branch:
        parts.append(f"branch={facts.branch}")
    if facts.commits_ahead is not None:
        parts.append(f"commits_ahead={facts.commits_ahead}")
    if facts.merge_conflicts is not None:
        parts.append(f"merge_conflicts={facts.merge_conflicts}")
    if facts.files_touched is not None:
        parts.append(f"files_touched={facts.files_touched}")
    if facts.headline:
        parts.append(f"— {facts.headline}")
    return " ".join(parts)


def from_digest_facts(facts: DigestFacts, *, project: str) -> MemoryCandidate:
    return MemoryCandidate(
        kind=MemoryKind.PROJECT,
        content=_summarize(facts),
        scope=Scope.TASK if facts.ref else Scope.PROJECT,
        trust=Trust.COCKPIT_MEASURED,
        confidence=CandidateConfidence.CONFIRMED,
        source="digest_facts",
        project_id=project,
        agent_id=facts.role,
        task_id=facts.ref,
    )
