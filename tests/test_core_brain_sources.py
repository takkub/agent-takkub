"""core.brain.sources.digest_facts_source — turns a done report's DigestFacts
into a MemoryCandidate (epic #309, Phase 7a)."""

from __future__ import annotations

from agent_takkub.core.brain.sources import digest_facts_source
from agent_takkub.core.models.memory import CandidateConfidence, Scope, Trust
from agent_takkub.digest_facts import DigestFacts

# ── digest_facts (in-memory, not file-backed) ───────────────────────────


def test_digest_facts_source_builds_a_cockpit_measured_candidate():
    facts = DigestFacts(
        role="backend",
        verdict="done",
        ref="#309",
        branch="wt/backend-2",
        commits_ahead=3,
        uncommitted=0,
        merge_conflicts=False,
        files_touched=5,
        files_dirs=("src", "tests"),
        headline="Second Brain candidate pipeline",
    )
    candidate = digest_facts_source.from_digest_facts(facts, project="demo")
    assert candidate.trust == Trust.COCKPIT_MEASURED
    assert candidate.confidence == CandidateConfidence.CONFIRMED
    assert candidate.scope == Scope.TASK
    assert candidate.task_id == "#309"
    assert candidate.agent_id == "backend"
    assert candidate.project_id == "demo"
    assert "branch=wt/backend-2" in candidate.content
    assert "Second Brain candidate pipeline" in candidate.content


def test_digest_facts_source_without_ref_scopes_to_project():
    facts = DigestFacts(role="qa", verdict="done")
    candidate = digest_facts_source.from_digest_facts(facts, project="demo")
    assert candidate.scope == Scope.PROJECT
    assert candidate.task_id is None
