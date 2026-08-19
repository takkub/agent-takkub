"""core.brain.retrieval — RetrievalEngine ranking: bm25 + scope match +
recency + confidence + importance, deterministic given a fixed `now`
(epic #309, Phase 7b)."""

from __future__ import annotations

import pytest

from agent_takkub.core.brain.retrieval import RetrievalEngine
from agent_takkub.core.brain.store import BrainStore
from agent_takkub.core.models.memory import (
    Confidence,
    MemoryKind,
    MemoryRecord,
    Scope,
    Trust,
)

_NOW = 1_755_000_000.0
_DAY = 86400.0


@pytest.fixture
def store(tmp_path, monkeypatch):
    import agent_takkub.config as config

    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)
    return BrainStore("demo")


def _rec(**kwargs) -> MemoryRecord:
    defaults = dict(
        id=kwargs.pop("id", "r"),
        kind=MemoryKind.PROJECT,
        content="x",
        created_at=_NOW,
    )
    defaults.update(kwargs)
    return MemoryRecord(**defaults)


def test_recall_on_empty_store_returns_nothing(store):
    engine = RetrievalEngine(store)
    assert engine.recall("anything", scope=Scope.PROJECT, now=_NOW) == []


def test_bm25_relevant_record_outranks_irrelevant_one(store):
    store.append_event(
        _rec(id="a", content="backend uses codex as its default provider", created_at=_NOW)
    )
    store.append_event(_rec(id="b", content="database migrations run via alembic", created_at=_NOW))
    engine = RetrievalEngine(store)
    ranked = engine.recall("codex provider", scope=Scope.PROJECT, now=_NOW)
    assert next(r.id for r in ranked) == "a"


def test_scope_match_breaks_a_bm25_tie(store):
    # Neither record's content matches the query at all (bm25 == 0 for
    # both), so scope match is the only signal that can separate them.
    store.append_event(_rec(id="a", content="unrelated text one", scope=Scope.AGENT))
    store.append_event(_rec(id="b", content="unrelated text two", scope=Scope.PROJECT))
    engine = RetrievalEngine(store)
    ranked = engine.recall("zzz-no-match", scope=Scope.PROJECT, now=_NOW)
    assert [r.id for r in ranked] == ["b", "a"]


def test_recency_prefers_the_newer_record_on_a_bm25_tie(store):
    store.append_event(_rec(id="old", content="same wording here", created_at=_NOW - 60 * _DAY))
    store.append_event(_rec(id="new", content="same wording here", created_at=_NOW - 1 * _DAY))
    engine = RetrievalEngine(store)
    ranked = engine.recall("wording", scope=Scope.PROJECT, now=_NOW)
    assert [r.id for r in ranked] == ["new", "old"]


def test_higher_trust_and_confidence_outranks_lower_on_a_bm25_tie(store):
    store.append_event(
        _rec(
            id="weak",
            content="shared wording token",
            trust=Trust.EXTERNAL_UNTRUSTED,
            confidence=Confidence.LOW,
        )
    )
    store.append_event(
        _rec(
            id="strong",
            content="shared wording token",
            trust=Trust.COCKPIT_MEASURED,
            confidence=Confidence.HIGH,
        )
    )
    engine = RetrievalEngine(store)
    ranked = engine.recall("wording", scope=Scope.PROJECT, now=_NOW)
    assert [r.id for r in ranked] == ["strong", "weak"]


def test_budget_tokens_trims_but_always_keeps_the_top_hit(store):
    long_content = "matching keyword " + ("filler word " * 200)
    store.append_event(_rec(id="a", content=long_content))
    store.append_event(_rec(id="b", content="matching keyword short"))
    engine = RetrievalEngine(store)
    ranked = engine.recall("matching keyword", scope=Scope.PROJECT, budget_tokens=1, now=_NOW)
    assert len(ranked) == 1


def test_zero_budget_returns_nothing(store):
    store.append_event(_rec(id="a", content="matching keyword"))
    engine = RetrievalEngine(store)
    assert engine.recall("matching", scope=Scope.PROJECT, budget_tokens=0, now=_NOW) == []


def test_recall_is_deterministic_across_repeated_calls(store):
    for i in range(5):
        store.append_event(_rec(id=f"r{i}", content=f"fact number {i} about the project"))
    engine = RetrievalEngine(store)
    first = [r.id for r in engine.recall("project fact", scope=Scope.PROJECT, now=_NOW)]
    second = [r.id for r in engine.recall("project fact", scope=Scope.PROJECT, now=_NOW)]
    assert first == second
