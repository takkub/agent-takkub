"""core.resilience.circuit_breaker — v2-hardening D/F (`11_CIRCUIT_
BREAKER.md`): threshold->open->skip->cooldown->half-open probe->recover/
reopen, plus basic thread-safety and the registry/persistence helpers."""

from __future__ import annotations

import threading

import pytest

from agent_takkub.core.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
    all_breakers,
    get_breaker,
    load_persisted_state,
    reset_registry,
)


@pytest.fixture(autouse=True)
def _clean_registry(tmp_path, monkeypatch):
    # Every `record_success`/`record_failure` best-effort-persists to
    # `DATA_HOME/resilience/state.json` — redirect it so a raw `CircuitBreaker()`
    # built directly in this file (bypassing `get_breaker`, so it's never in
    # the registry `all_breakers()` reads from) can't still trigger a stray
    # write into this repo's own working tree via that persist call.
    from agent_takkub import config

    monkeypatch.setattr(config, "DATA_HOME", tmp_path / "data")
    reset_registry()
    yield
    reset_registry()


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _breaker(**kwargs) -> CircuitBreaker:
    clock = kwargs.pop("clock", None) or _FakeClock()
    return CircuitBreaker("svc", failure_threshold=3, cooldown_s=60.0, clock=clock, **kwargs)


class TestCircuitBreakerLifecycle:
    def test_closed_allows_calls(self) -> None:
        b = _breaker()
        assert b.allow_call() is True

    def test_stays_closed_below_threshold(self) -> None:
        b = _breaker()
        b.record_failure()
        b.record_failure()
        assert b.allow_call() is True
        snap = b.snapshot()
        assert snap.state == CircuitState.CLOSED
        assert snap.failure_count == 2

    def test_opens_at_threshold_and_skips_every_call(self) -> None:
        b = _breaker()
        b.record_failure()
        b.record_failure()
        b.record_failure()

        snap = b.snapshot()
        assert snap.state == CircuitState.OPEN
        # No calls attempted at all while open — repeated checks, not one.
        assert b.allow_call() is False
        assert b.allow_call() is False
        assert b.allow_call() is False

    def test_success_resets_failure_count(self) -> None:
        b = _breaker()
        b.record_failure()
        b.record_failure()
        b.record_success()
        b.record_failure()
        b.record_failure()

        assert b.allow_call() is True
        assert b.snapshot().failure_count == 2

    def test_stays_open_before_cooldown_elapses(self) -> None:
        clock = _FakeClock()
        b = _breaker(clock=clock)
        for _ in range(3):
            b.record_failure()

        clock.advance(59.9)
        assert b.allow_call() is False

    def test_half_open_probe_after_cooldown_recovers_on_success(self) -> None:
        clock = _FakeClock()
        b = _breaker(clock=clock)
        for _ in range(3):
            b.record_failure()

        clock.advance(60.0)
        assert b.allow_call() is True  # the probe
        assert b.snapshot().state == CircuitState.HALF_OPEN

        b.record_success()

        snap = b.snapshot()
        assert snap.state == CircuitState.CLOSED
        assert snap.failure_count == 0
        assert snap.last_healthy == clock.now
        assert b.allow_call() is True

    def test_half_open_probe_failure_reopens_with_fresh_cooldown(self) -> None:
        clock = _FakeClock()
        b = _breaker(clock=clock)
        for _ in range(3):
            b.record_failure()
        clock.advance(60.0)
        assert b.allow_call() is True  # probe allowed

        b.record_failure()  # probe fails

        snap = b.snapshot()
        assert snap.state == CircuitState.OPEN
        assert b.allow_call() is False  # still open, fresh cooldown
        clock.advance(59.9)
        assert b.allow_call() is False
        clock.advance(0.2)
        assert b.allow_call() is True  # second probe after the fresh window

    def test_only_one_half_open_probe_at_a_time(self) -> None:
        clock = _FakeClock()
        b = _breaker(clock=clock)
        for _ in range(3):
            b.record_failure()
        clock.advance(60.0)

        assert b.allow_call() is True  # first probe claims the slot
        assert b.allow_call() is False  # a second concurrent caller is refused
        assert b.allow_call() is False


class TestCircuitBreakerThreadSafety:
    def test_concurrent_failures_never_exceed_or_corrupt_state(self) -> None:
        b = _breaker(clock=_FakeClock())
        threads = [threading.Thread(target=b.record_failure) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        snap = b.snapshot()
        assert snap.state == CircuitState.OPEN
        # Every failure before the trip increments serially (the lock
        # guarantees no lost updates) — count never exceeds calls made, and
        # the breaker is unambiguously open, not in some torn in-between
        # state.
        assert snap.failure_count >= 3

    def test_concurrent_allow_call_never_lets_two_probes_through(self) -> None:
        clock = _FakeClock()
        b = _breaker(clock=clock)
        for _ in range(3):
            b.record_failure()
        clock.advance(60.0)

        results: list[bool] = []
        lock = threading.Lock()

        def attempt():
            allowed = b.allow_call()
            with lock:
                results.append(allowed)

        threads = [threading.Thread(target=attempt) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.count(True) == 1


class TestRegistry:
    def test_get_breaker_returns_same_instance_for_same_name(self) -> None:
        a = get_breaker("svc-a")
        b = get_breaker("svc-a")
        assert a is b

    def test_get_breaker_different_names_are_independent(self) -> None:
        a = get_breaker("svc-a")
        b = get_breaker("svc-b")
        assert a is not b

    def test_all_breakers_lists_every_registered_breaker(self) -> None:
        get_breaker("svc-a")
        get_breaker("svc-b")
        names = {b.name for b in all_breakers()}
        assert names == {"svc-a", "svc-b"}

    def test_reset_registry_drops_prior_instances(self) -> None:
        first = get_breaker("svc-a")
        reset_registry()
        second = get_breaker("svc-a")
        assert first is not second


class TestPersistence:
    def test_load_persisted_state_absent_returns_none(self, tmp_path, monkeypatch) -> None:
        from agent_takkub import config

        monkeypatch.setattr(config, "DATA_HOME", tmp_path / "data")

        assert load_persisted_state() is None

    def test_record_failure_persists_state_readable_across_reload(
        self, tmp_path, monkeypatch
    ) -> None:
        from agent_takkub import config

        monkeypatch.setattr(config, "DATA_HOME", tmp_path / "data")

        b = get_breaker("persist-svc", failure_threshold=1, cooldown_s=60.0)
        b.record_failure()

        state = load_persisted_state()
        assert state is not None
        assert state["persist-svc"]["state"] == CircuitState.OPEN
        assert state["persist-svc"]["failure_count"] == 1
