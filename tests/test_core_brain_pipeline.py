"""core.brain.pipeline — MemoryManager candidate pipeline: dedup, conflict
detect + supersede (never deletes), scope/confidence mapping, versioning,
persistence via BrainStore (epic #309, Phase 7a)."""

from __future__ import annotations

import pytest

from agent_takkub.core.brain.candidate import MemoryCandidate
from agent_takkub.core.brain.pipeline import MemoryManager, SubmitStatus
from agent_takkub.core.brain.store import BrainStore
from agent_takkub.core.models.memory import (
    CandidateConfidence,
    Confidence,
    MemoryKind,
    RecordStatus,
    Scope,
    Trust,
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    import agent_takkub.config as config

    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)
    return BrainStore("demo")


@pytest.fixture
def manager(store):
    return MemoryManager(store)


def test_reject_too_short_content(manager):
    result = manager.submit_candidate(MemoryCandidate(kind=MemoryKind.PROJECT, content="ok"))
    assert result.status == SubmitStatus.REJECTED
    assert result.record is None


def test_first_submit_creates_a_record(manager):
    result = manager.submit_candidate(
        MemoryCandidate(
            kind=MemoryKind.PROJECT,
            content="backend uses codex as its default provider",
            project_id="demo",
        )
    )
    assert result.status == SubmitStatus.CREATED
    assert result.record.version == 1
    assert result.record.status == RecordStatus.ACTIVE


def test_exact_duplicate_is_a_no_op(manager):
    candidate = MemoryCandidate(
        kind=MemoryKind.PROJECT,
        content="backend uses codex as its default provider",
        project_id="demo",
    )
    first = manager.submit_candidate(candidate)
    second = manager.submit_candidate(candidate)
    assert second.status == SubmitStatus.DUPLICATE
    assert second.record.id == first.record.id


def test_near_duplicate_wording_is_still_a_duplicate(manager):
    manager.submit_candidate(
        MemoryCandidate(
            kind=MemoryKind.PROJECT,
            content="backend uses codex as its default provider",
            project_id="demo",
        )
    )
    result = manager.submit_candidate(
        MemoryCandidate(
            kind=MemoryKind.PROJECT,
            content="  Backend  uses codex as its default provider!  ",
            project_id="demo",
        )
    )
    assert result.status == SubmitStatus.DUPLICATE


def test_conflicting_update_supersedes_the_old_record(manager, store):
    first = manager.submit_candidate(
        MemoryCandidate(
            kind=MemoryKind.PROJECT,
            content="backend uses codex as its default provider",
            project_id="demo",
        )
    )
    second = manager.submit_candidate(
        MemoryCandidate(
            kind=MemoryKind.PROJECT,
            content="backend uses gemini as its default provider",
            project_id="demo",
        )
    )
    assert second.status == SubmitStatus.SUPERSEDED
    assert second.record.version == 2

    all_records = {r.id: r for r in store.load_latest()}
    old = all_records[first.record.id]
    assert old.status == RecordStatus.SUPERSEDED
    assert old.superseded_by == second.record.id
    # History is never deleted — the old record is still readable.
    assert old.content == "backend uses codex as its default provider"

    active = store.load_active()
    assert [r.id for r in active] == [second.record.id]


def test_unrelated_fact_is_not_treated_as_a_conflict(manager, store):
    manager.submit_candidate(
        MemoryCandidate(
            kind=MemoryKind.PROJECT, content="backend uses codex provider", project_id="demo"
        )
    )
    result = manager.submit_candidate(
        MemoryCandidate(
            kind=MemoryKind.PROJECT,
            content="database migrations run via alembic upgrade head",
            project_id="demo",
        )
    )
    assert result.status == SubmitStatus.CREATED
    assert len(store.load_active()) == 2


def test_dedup_is_scoped_to_kind_scope_project_and_agent(manager, store):
    """Same normalized text in a DIFFERENT bucket (different project) must
    not dedup against an unrelated bucket's record."""
    manager.submit_candidate(
        MemoryCandidate(
            kind=MemoryKind.PROJECT,
            content="run tests with pytest -x",
            project_id="proj-a",
        )
    )
    result = manager.submit_candidate(
        MemoryCandidate(
            kind=MemoryKind.PROJECT,
            content="run tests with pytest -x",
            project_id="proj-b",
        )
    )
    assert result.status == SubmitStatus.CREATED


@pytest.mark.parametrize(
    "candidate_confidence,expected",
    [
        (CandidateConfidence.CONFIRMED, Confidence.HIGH),
        (CandidateConfidence.HIGH, Confidence.HIGH),
        (CandidateConfidence.INFERRED, Confidence.MEDIUM),
        (CandidateConfidence.UNVERIFIED, Confidence.LOW),
    ],
)
def test_candidate_confidence_maps_down_to_record_confidence(
    manager, candidate_confidence, expected
):
    result = manager.submit_candidate(
        MemoryCandidate(
            kind=MemoryKind.PROJECT,
            content="a fact worth remembering about this project",
            confidence=candidate_confidence,
        )
    )
    assert result.record.confidence == expected


def test_scope_and_trust_pass_through_unchanged(manager):
    result = manager.submit_candidate(
        MemoryCandidate(
            kind=MemoryKind.FEEDBACK,
            content="never mock the database in integration tests",
            scope=Scope.AGENT,
            trust=Trust.LEAD_CONFIRMED,
            agent_id="backend",
            project_id="demo",
        )
    )
    assert result.record.scope == Scope.AGENT
    assert result.record.trust == Trust.LEAD_CONFIRMED
    assert result.record.agent_id == "backend"


def test_persistence_round_trips_across_manager_instances(tmp_path, monkeypatch):
    import agent_takkub.config as config

    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)
    store_a = BrainStore("demo")
    MemoryManager(store_a).submit_candidate(
        MemoryCandidate(
            kind=MemoryKind.PROJECT, content="a durable fact worth keeping", project_id="demo"
        )
    )
    store_b = BrainStore("demo")
    assert len(store_b.load_active()) == 1
