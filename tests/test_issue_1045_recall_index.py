"""#1045/#1069: recall keeps what it derives from a store until the store
changes, and a store that only grew is read from where it left off."""

from __future__ import annotations

import dataclasses
import os

from agent_takkub.core.brain import retrieval
from agent_takkub.core.brain.retrieval import RetrievalEngine
from agent_takkub.core.brain.store import BrainStore
from agent_takkub.core.models.memory import (
    Confidence,
    MemoryKind,
    MemoryRecord,
    RecordStatus,
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


def _bump_mtime(path) -> None:
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))


def _same_as_full_rebuild(store: BrainStore) -> None:
    got = retrieval._index_for(store)
    want = retrieval._build_index(store.load_active())
    assert [r.id for r in got.records] == [r.id for r in want.records]
    assert got.tfs == want.tfs and got.df == want.df and got.doc_len == want.doc_len


def test_second_recall_reads_nothing_until_the_store_file_changes(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_takkub.core.brain.store.core_home", lambda: tmp_path)
    monkeypatch.setattr(retrieval, "_INDEX_CACHE", {})
    store = BrainStore("proj")
    store.append_event(_record(1, "payment webhook retries use exponential backoff"))
    store.append_event(_record(2, "login page uses the shared form component"))

    reads: list[int] = []
    real_read = retrieval._read_new_rows

    def _counting(path, state):
        out = real_read(path, state)
        reads.append(len(out[0]))
        return out

    monkeypatch.setattr(retrieval, "_read_new_rows", _counting)
    engine = RetrievalEngine(BrainStore("proj"))
    first = engine.recall("payment webhook backoff", scope=Scope.PROJECT)
    again = RetrievalEngine(BrainStore("proj")).recall(
        "payment webhook backoff", scope=Scope.PROJECT
    )
    assert [r.id for r in first] == [r.id for r in again] == ["r1"]
    assert reads == [2], "an unchanged store must not be re-read and re-counted"

    store.append_event(_record(3, "payment webhook signature is checked before the retry"))
    _bump_mtime(store.path)
    found = engine.recall("payment webhook signature", scope=Scope.PROJECT)
    assert "r3" in [r.id for r in found]
    assert reads == [2, 1], "#1069: a grown store is read from the last offset only"


def test_incremental_index_matches_a_full_rebuild_through_supersede_and_rewrite(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("agent_takkub.core.brain.store.core_home", lambda: tmp_path)
    monkeypatch.setattr(retrieval, "_INDEX_CACHE", {})
    store = BrainStore("proj")
    r1 = _record(1, "payment webhook retries use exponential backoff")
    store.append_event(r1)
    store.append_event(_record(2, "login page uses the shared form component"))
    _same_as_full_rebuild(store)

    store.append_event(dataclasses.replace(r1, status=RecordStatus.SUPERSEDED))
    store.append_event(_record(3, "payment webhook signature is checked"))
    _bump_mtime(store.path)
    _same_as_full_rebuild(store)
    assert "r1" not in [r.id for r in retrieval._index_for(store).records]

    # Not an append: the file was replaced with different content.
    store.path.write_bytes(b"")
    store.append_event(_record(9, "a different store entirely"))
    _bump_mtime(store.path)
    _same_as_full_rebuild(store)
    assert [r.id for r in retrieval._index_for(store).records] == ["r9"]
