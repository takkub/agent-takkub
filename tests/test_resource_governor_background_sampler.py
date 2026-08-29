"""#437: the default (psutil) sampler must never run on the caller's thread.

`ResourceGovernor.sample()` is called from the Qt tick; `_sample_psutil`'s
`psutil.pids()` walks the whole process table and showed up in a main-thread
stall dump. `BackgroundSampler` reads on a worker and hands back a cache.
"""

from __future__ import annotations

import threading
import time

from agent_takkub.resource_governor import BackgroundSampler, ResourceGovernor


def test_first_call_returns_fallback_without_blocking_on_the_read() -> None:
    gate = threading.Event()
    calls: list[str] = []

    def slow_read() -> tuple:
        calls.append(threading.current_thread().name)
        gate.wait(5)
        return (42.0, 50.0, 7, 1, 2)

    sampler = BackgroundSampler(slow_read, interval_s=0.01, fallback=lambda: (0.0, 100.0, 0, 0, 0))
    try:
        t0 = time.monotonic()
        first = sampler()
        assert time.monotonic() - t0 < 1.0
        assert first == (0.0, 100.0, 0, 0, 0)
        gate.set()
        deadline = time.monotonic() + 5
        while sampler() == (0.0, 100.0, 0, 0, 0) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert sampler() == (42.0, 50.0, 7, 1, 2)
        assert calls and all(name == "resource-governor-sampler" for name in calls)
        assert "MainThread" not in calls
    finally:
        sampler.stop()


def test_constructing_a_governor_starts_no_thread_until_sampled() -> None:
    before = {t.name for t in threading.enumerate()}
    governor = ResourceGovernor()
    after = {t.name for t in threading.enumerate()}
    assert "resource-governor-sampler" not in (after - before)
    assert isinstance(governor._sampler, BackgroundSampler)
    governor._sampler.stop()


def test_a_read_that_raises_keeps_the_last_good_value() -> None:
    values = iter([(1.0, 1.0, 1, 1, 1)])

    def read() -> tuple:
        return next(values)  # StopIteration on the second call

    sampler = BackgroundSampler(read, interval_s=0.01, fallback=lambda: (0.0, 0.0, 0, 0, 0))
    try:
        deadline = time.monotonic() + 5
        while sampler() != (1.0, 1.0, 1, 1, 1) and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)
        assert sampler() == (1.0, 1.0, 1, 1, 1)
    finally:
        sampler.stop()
