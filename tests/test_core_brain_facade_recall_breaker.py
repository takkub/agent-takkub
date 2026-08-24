"""core.brain.facade.recall — v2-hardening D/F: the brain-read path is wired
to `core.resilience.circuit_breaker` (`_BRAIN_READ_BREAKER_NAME`), with a
much higher failure_threshold than the network clients in design_clients
since this is a local disk store, not a remote service (see the module's own
comment for the reasoning). `RetrievalEngine` itself is faked out entirely —
this only proves the breaker wiring, not `RetrievalEngine`'s own behavior
(covered by test_core_brain_retrieval.py)."""

from __future__ import annotations

import pytest

import agent_takkub.core.brain.facade as facade
from agent_takkub.core.models.memory import MemoryKind, MemoryRecord
from agent_takkub.core.resilience.circuit_breaker import (
    CircuitState,
    get_breaker,
    reset_registry,
)


@pytest.fixture(autouse=True)
def _runtime(tmp_path, monkeypatch):
    from agent_takkub import config

    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_HOME", tmp_path / "data")
    monkeypatch.setenv("TAKKUB_V2_BRAIN", "1")
    reset_registry()
    yield
    reset_registry()


class _RaisingEngine:
    def __init__(self, _store) -> None:
        pass

    def recall(self, *_args, **_kwargs):
        raise RuntimeError("simulated store failure")


class _CountingRaisingEngine:
    calls = 0

    def __init__(self, _store) -> None:
        pass

    def recall(self, *_args, **_kwargs):
        type(self).calls += 1
        raise RuntimeError("simulated store failure")


class _OkEngine:
    def __init__(self, _store) -> None:
        pass

    def recall(self, *_args, **_kwargs):
        return [MemoryRecord(id="r1", kind=MemoryKind.PROJECT, content="fact")]


def test_repeated_failures_open_breaker_then_engine_stops_being_constructed(monkeypatch) -> None:
    _CountingRaisingEngine.calls = 0
    monkeypatch.setattr(facade, "RetrievalEngine", _CountingRaisingEngine)

    for _ in range(10):
        assert facade.recall("q", project="p") == []
    assert _CountingRaisingEngine.calls == 10
    assert get_breaker(facade._BRAIN_READ_BREAKER_NAME).snapshot().state == CircuitState.OPEN

    # Circuit now open — recall must degrade to [] without ever touching
    # RetrievalEngine.recall again.
    for _ in range(5):
        assert facade.recall("q", project="p") == []
    assert _CountingRaisingEngine.calls == 10


def test_success_returns_records_and_keeps_breaker_closed(monkeypatch) -> None:
    monkeypatch.setattr(facade, "RetrievalEngine", _OkEngine)

    results = facade.recall("q", project="p")

    assert len(results) == 1
    assert get_breaker(facade._BRAIN_READ_BREAKER_NAME).snapshot().state == CircuitState.CLOSED


def test_success_after_failures_resets_failure_count(monkeypatch) -> None:
    monkeypatch.setattr(facade, "RetrievalEngine", _RaisingEngine)
    for _ in range(5):
        facade.recall("q", project="p")
    assert get_breaker(facade._BRAIN_READ_BREAKER_NAME).snapshot().failure_count == 5

    monkeypatch.setattr(facade, "RetrievalEngine", _OkEngine)
    facade.recall("q", project="p")

    assert get_breaker(facade._BRAIN_READ_BREAKER_NAME).snapshot().failure_count == 0
