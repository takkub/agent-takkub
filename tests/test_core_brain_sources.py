"""core.brain.sources — ingest adapters that turn existing cockpit text
sources into MemoryCandidates, exercised against realistic fixtures shaped
like the real writers' output (role_memory.py, vault_mirror.py's knowledge
base + decision notes, digest_facts.py) (epic #309, Phase 7a)."""

from __future__ import annotations

from agent_takkub.core.brain.sources import (
    decision_note_source,
    digest_facts_source,
    knowledge_source,
    role_memory_source,
)
from agent_takkub.core.models.memory import CandidateConfidence, MemoryKind, Scope, Trust
from agent_takkub.digest_facts import DigestFacts

# ── role_memory ──────────────────────────────────────────────────────────

_ROLE_MEMORY_FIXTURE = """# backend — learned notes · project: demo

> some header prose

## Conventions / patterns
- (ว่าง — เติมเมื่อเรียนรู้)

## Gotchas / pitfalls
- editable install points at a different checkout, use PYTHONPATH instead
- the dev server watcher fights Write/Edit — kill it before bulk edits

## Key decisions / เหตุผล
-
"""


def test_role_memory_source_parses_real_bullets_and_skips_seed_placeholder():
    candidates = role_memory_source.from_role_memory_text(
        _ROLE_MEMORY_FIXTURE, project="demo", role="backend"
    )
    contents = [c.content for c in candidates]
    assert "editable install points at a different checkout, use PYTHONPATH instead" in contents
    assert "the dev server watcher fights Write/Edit — kill it before bulk edits" in contents
    assert not any("ว่าง" in c for c in contents)
    assert len(candidates) == 2
    for c in candidates:
        assert c.kind == MemoryKind.PROJECT
        assert c.scope == Scope.AGENT
        assert c.trust == Trust.AGENT_REPORTED
        assert c.source == "role_memory"
        assert c.project_id == "demo"
        assert c.agent_id == "backend"


def test_role_memory_source_empty_seed_yields_nothing():
    seed_only = "# role — learned notes\n\n## Gotchas / pitfalls\n-\n"
    assert role_memory_source.from_role_memory_text(seed_only, project="demo", role="qa") == []


# ── knowledge base ───────────────────────────────────────────────────────

_KNOWLEDGE_FIXTURE = (
    "# demo — knowledge base\n\n"
    "> Auto-distilled durable facts\n\n"
    "- `2026-08-10T12:00:00` **backend** — fixed #206 by draining the request body first\n"
    "- `2026-08-11T09:30:00` **qa** — flaky login test needs a 2s wait after redirect\n"
)


def test_knowledge_source_parses_entries_with_timestamp_and_role():
    candidates = knowledge_source.from_knowledge_base_text(_KNOWLEDGE_FIXTURE, project="demo")
    assert len(candidates) == 2
    first = candidates[0]
    assert first.content == "fixed #206 by draining the request body first"
    assert first.agent_id == "backend"
    assert first.project_id == "demo"
    assert first.trust == Trust.AGENT_REPORTED
    assert first.confidence == CandidateConfidence.HIGH
    assert first.source == "knowledge_base"
    assert first.created_at is not None


def test_knowledge_source_ignores_non_entry_lines():
    text = "# header\n\n> a blockquote, not an entry\nplain prose line\n"
    assert knowledge_source.from_knowledge_base_text(text, project="demo") == []


# ── decision notes ───────────────────────────────────────────────────────

_DECISION_NOTE_FIXTURE = """---
role: backend
project: demo
date: 2026-08-15T14:22:05
tags: [session, backend, demo]
---

# backend done · 2026-08-15T14:22:05

**Role:** backend

## Note

Switched the retry policy to exponential backoff after the 3rd 429.

## Transcript

Raw byte stream (with ANSI): `runtime/sessions/2026-08-15/demo/backend-142205.raw`
"""


def test_decision_note_source_parses_frontmatter_and_note_body():
    candidate = decision_note_source.from_decision_note_text(_DECISION_NOTE_FIXTURE)
    assert candidate is not None
    assert (
        candidate.content == "Switched the retry policy to exponential backoff after the 3rd 429."
    )
    assert candidate.project_id == "demo"
    assert candidate.agent_id == "backend"
    assert candidate.source == "decision_note"
    assert candidate.trust == Trust.AGENT_REPORTED
    assert candidate.created_at is not None


def test_decision_note_source_returns_none_without_a_note_section():
    text = "---\nrole: backend\nproject: demo\n---\n\n# no note here\n"
    assert decision_note_source.from_decision_note_text(text) is None


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
