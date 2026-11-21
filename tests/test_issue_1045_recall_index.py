"""#1045: recall keeps what it derives from a store until the store changes."""

from __future__ import annotations

import os

from agent_takkub.core.brain import retrieval
from agent_takkub.core.brain.retrieval import RetrievalEngine
from agent_takkub.core.brain.store import BrainStore
from agent_takkub.core.models.memory import (
    Confidence,
    MemoryKind,
    MemoryRecord,
    Scope,
    Trust,
)


def _record(i: int, content: str) -> MemoryRecord:
    return MemoryRecord(
        id=f"r{i}",
        kind=MemoryKind.PROJECT,
        scope=Scope.PROJECT,
        content=content,
        source="test",
        trust=Trust.COCKPIT_MEASURED,
        confidence=Confidence.HIGH,
        created_at=1_000_000.0 + i,
    )


def test_second_recall_reads_nothing_until_the_store_file_changes(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_takkub.core.brain.store.core_home", lambda: tmp_path)
    monkeypatch.setattr(retrieval, "_INDEX_CACHE", {})
    store = BrainStore("proj")
    store.append_event(_record(1, "payment webhook retries use exponential backoff"))
    store.append_event(_record(2, "login page uses the shared form component"))

    loads = []
    real_load = BrainStore.load_active
    monkeypatch.setattr(BrainStore, "load_active", lambda self: loads.append(1) or real_load(self))
    engine = RetrievalEngine(BrainStore("proj"))
    first = engine.recall("payment webhook backoff", scope=Scope.PROJECT)
    again = RetrievalEngine(BrainStore("proj")).recall(
        "payment webhook backoff", scope=Scope.PROJECT
    )
    assert [r.id for r in first] == [r.id for r in again] == ["r1"]
    assert len(loads) == 1, "an unchanged store must not be re-read and re-counted"

    store.append_event(_record(3, "payment webhook signature is checked before the retry"))
    st = os.stat(store.path)
    os.utime(store.path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))
    found = engine.recall("payment webhook signature", scope=Scope.PROJECT)
    assert "r3" in [r.id for r in found] and len(loads) == 2


def test_indexed_scores_match_the_plain_bm25():
    docs = [["a", "b", "b"], ["c"], ["a", "c", "d"]]
    tfs, df = retrieval._term_counts(docs)
    assert retrieval._bm25_from_counts(
        tfs, df, [len(d) for d in docs], ["a", "b"]
    ) == retrieval._bm25_scores(docs, ["a", "b"])
