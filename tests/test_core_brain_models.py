"""core.models.memory extensions for Phase 7a — Scope gained user/task/
session, MemoryRecord gained supersede fields, all backward-compatible
with the existing (Phase 1) construction and defaults asserted by
test_core_models.py."""

from __future__ import annotations

import dataclasses

from agent_takkub.core.models.memory import (
    CONFIDENCE_WEIGHT,
    CandidateConfidence,
    Confidence,
    MemoryKind,
    MemoryRecord,
    RecordStatus,
    Scope,
    confidence_for,
)


def test_scope_gained_user_task_session():
    assert Scope.USER == "user"
    assert Scope.TASK == "task"
    assert Scope.SESSION == "session"
    # Original 4 members untouched.
    assert {Scope.GLOBAL, Scope.WORKSPACE, Scope.PROJECT, Scope.AGENT} == {
        "global",
        "workspace",
        "project",
        "agent",
    }


def test_memory_record_still_defaults_the_same_as_phase1():
    """No behavior change for callers built against the original 10 fields."""
    record = MemoryRecord(id="m1", kind=MemoryKind.FEEDBACK, content="never mock the db")
    assert record.trust.value == "agent_reported"
    assert record.confidence == Confidence.MEDIUM
    assert record.scope == Scope.PROJECT
    assert record.user_id is None
    assert record.workspace_id is None
    assert record.project_id is None


def test_memory_record_supersede_fields_default_to_active_unsuperseded():
    record = MemoryRecord(id="m1", kind=MemoryKind.PROJECT, content="a fact")
    assert record.status == RecordStatus.ACTIVE
    assert record.superseded_by is None
    assert record.version == 1
    assert record.source == "unknown"


def test_memory_record_is_still_frozen_and_slotted():
    assert dataclasses.is_dataclass(MemoryRecord)
    assert MemoryRecord.__dataclass_params__.frozen is True
    assert "__slots__" in MemoryRecord.__dict__


def test_candidate_confidence_weights_are_ordered_and_bounded():
    weights = [CONFIDENCE_WEIGHT[c] for c in CandidateConfidence]
    assert weights == sorted(weights, reverse=True)
    assert all(0.0 < w <= 1.0 for w in weights)
    assert CONFIDENCE_WEIGHT[CandidateConfidence.CONFIRMED] == 1.0
    assert CONFIDENCE_WEIGHT[CandidateConfidence.UNVERIFIED] == 0.4


def test_confidence_for_maps_every_candidate_level():
    assert confidence_for(CandidateConfidence.CONFIRMED) == Confidence.HIGH
    assert confidence_for(CandidateConfidence.HIGH) == Confidence.HIGH
    assert confidence_for(CandidateConfidence.INFERRED) == Confidence.MEDIUM
    assert confidence_for(CandidateConfidence.UNVERIFIED) == Confidence.LOW
