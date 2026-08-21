"""core.brain.adapter / facade — NativeBrainAdapter satisfies the
BrainAdapter contract end-to-end, and the facade is flag-gated + fail-open
(epic #309, Phase 7a/7b)."""

from __future__ import annotations

import pytest

import agent_takkub.core.brain.facade as facade
from agent_takkub.core.brain.adapter import NativeBrainAdapter
from agent_takkub.core.brain.candidate import MemoryCandidate
from agent_takkub.core.brain.pipeline import MemoryManager
from agent_takkub.core.brain.retrieval import RetrievalEngine
from agent_takkub.core.brain.store import BrainStore
from agent_takkub.core.contracts.brain_adapter import BrainAdapter
from agent_takkub.core.models.memory import Confidence, MemoryKind, MemoryRecord, Scope, Trust


@pytest.fixture
def brain_store(tmp_path, monkeypatch):
    import agent_takkub.config as config

    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)
    return BrainStore("demo")


@pytest.fixture
def adapter(brain_store):
    return NativeBrainAdapter(MemoryManager(brain_store), RetrievalEngine(brain_store))


def test_native_adapter_satisfies_the_brain_adapter_protocol(adapter):
    assert isinstance(adapter, BrainAdapter)


def test_remember_then_recall_round_trips(adapter):
    adapter.remember(
        MemoryRecord(
            id="ignored-caller-cannot-set-this-directly",
            kind=MemoryKind.PROJECT,
            content="backend uses codex as its default provider",
            scope=Scope.PROJECT,
            trust=Trust.LEAD_CONFIRMED,
            confidence=Confidence.HIGH,
        )
    )
    hits = adapter.recall("codex provider", scope=Scope.PROJECT)
    assert len(hits) == 1
    assert hits[0].content == "backend uses codex as its default provider"
    # remember() went through the pipeline, not a raw write: the persisted
    # record got its own id, not the caller-supplied one.
    assert hits[0].id != "ignored-caller-cannot-set-this-directly"
    assert hits[0].trust == Trust.LEAD_CONFIRMED


def test_remember_deduplicates_through_the_pipeline(adapter, brain_store):
    record = MemoryRecord(id="a", kind=MemoryKind.PROJECT, content="a durable fact here")
    adapter.remember(record)
    adapter.remember(record)
    assert len(brain_store.load_active()) == 1


def test_recall_respects_limit(adapter):
    facts = [
        "fact about the database migrations",
        "fact about the auth middleware rewrite",
        "fact about the retry backoff policy",
        "fact about the deploy pipeline caching",
        "fact about the websocket reconnect logic",
    ]
    for i, content in enumerate(facts):
        adapter.remember(MemoryRecord(id=f"r{i}", kind=MemoryKind.PROJECT, content=content))
    assert len(adapter.recall("fact", scope=Scope.PROJECT, limit=2)) == 2


# ── facade: flag off / on / fail-open ────────────────────────────────────


def test_facade_flag_on_by_default(monkeypatch):
    """Default flipped ON in 1.0.84 (epic #309)."""
    monkeypatch.delenv("TAKKUB_V2_BRAIN", raising=False)
    from agent_takkub.core.brain.flag import v2_brain_enabled

    assert v2_brain_enabled() is True


def test_facade_recall_flag_off_returns_empty_without_touching_store(monkeypatch):
    monkeypatch.delenv("TAKKUB_V2_BRAIN", raising=False)

    def boom(*a, **kw):
        raise AssertionError("BrainStore must not be constructed when the flag is off")

    monkeypatch.setattr(facade, "BrainStore", boom)
    assert facade.recall("query") == []


def test_facade_submit_flag_off_returns_none_without_touching_store(monkeypatch):
    monkeypatch.delenv("TAKKUB_V2_BRAIN", raising=False)

    def boom(*a, **kw):
        raise AssertionError("BrainStore must not be constructed when the flag is off")

    monkeypatch.setattr(facade, "BrainStore", boom)
    candidate = MemoryCandidate(kind=MemoryKind.PROJECT, content="a fact worth keeping")
    assert facade.submit(candidate) is None


def test_facade_flag_on_submit_then_recall(monkeypatch, tmp_path):
    import agent_takkub.config as config

    monkeypatch.setenv("TAKKUB_V2_BRAIN", "1")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)
    candidate = MemoryCandidate(
        kind=MemoryKind.PROJECT, content="backend uses codex as its default provider"
    )
    result = facade.submit(candidate)
    assert result is not None
    assert result.record is not None
    hits = facade.recall("codex provider")
    assert len(hits) == 1


def test_facade_flag_on_fails_open_on_exception(monkeypatch, tmp_path):
    import agent_takkub.config as config

    monkeypatch.setenv("TAKKUB_V2_BRAIN", "1")
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)

    class _BoomStore:
        def __init__(self, *a, **kw):
            raise RuntimeError("store blew up")

    monkeypatch.setattr(facade, "BrainStore", _BoomStore)
    assert facade.recall("anything") == []
    candidate = MemoryCandidate(kind=MemoryKind.PROJECT, content="a fact worth keeping")
    assert facade.submit(candidate) is None
