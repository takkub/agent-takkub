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
    # Both records match the query identically, so scope match is the only
    # signal left that can separate them.
    store.append_event(_rec(id="a", content="same wording here", scope=Scope.AGENT))
    store.append_event(_rec(id="b", content="same wording here", scope=Scope.PROJECT))
    engine = RetrievalEngine(store)
    ranked = engine.recall("same wording here", scope=Scope.PROJECT, now=_NOW)
    assert [r.id for r in ranked] == ["b", "a"]


def test_record_with_no_query_overlap_is_not_returned(store):
    """#333: the non-query signals (scope+recency+confidence+trust) sum to
    1.4, above `_W_BM25` at full scale, so a record that matches nothing used
    to be returned anyway — recent+trusted beat relevant."""
    store.append_event(
        _rec(
            id="fresh",
            content="unrelated text one",
            scope=Scope.PROJECT,
            trust=Trust.COCKPIT_MEASURED,
            confidence=Confidence.HIGH,
        )
    )
    engine = RetrievalEngine(store)
    assert engine.recall("zzz-no-match", scope=Scope.PROJECT, now=_NOW) == []


def test_match_on_a_single_query_token_is_below_the_coverage_floor(store):
    """The measured failure mode: on a small store one rare token carries a
    high idf, so a record whose ONLY overlap with an 8-token query was the
    word "the" scored higher than a genuine multi-term match."""
    store.append_event(_rec(id="stopword", content="resolved the merge conflict"))
    store.append_event(_rec(id="real", content="add pagination to the users table endpoint"))
    engine = RetrievalEngine(store)
    ranked = engine.recall(
        "add pagination to the users table api endpoint", scope=Scope.PROJECT, now=_NOW
    )
    assert [r.id for r in ranked] == ["real"]


def test_blank_query_ranks_nothing(store):
    store.append_event(_rec(id="a", content="some content here"))
    engine = RetrievalEngine(store)
    assert engine.recall("   ", scope=Scope.PROJECT, now=_NOW) == []


def test_broader_query_coverage_outranks_a_higher_raw_bm25_score(store):
    """Ranking is coverage-weighted: matching more of what was asked beats
    matching one term very strongly (the old `bm25 / max(bm25)` rescaled the
    best raw hit to 1.0 however narrow it was)."""
    store.append_event(_rec(id="narrow", content="alembic alembic alembic alembic"))
    store.append_event(_rec(id="broad", content="alembic database migrations for the backend"))
    engine = RetrievalEngine(store)
    ranked = engine.recall("alembic database migrations backend", scope=Scope.PROJECT, now=_NOW)
    assert next(r.id for r in ranked) == "broad"


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
