"""#1061: what the idle tick may no longer do on the Qt main thread — wait on
the pool, read the usage record, or re-render a pane's screen every call."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from agent_takkub import bg_pool
from agent_takkub import orchestrator as orch_mod
from agent_takkub import pty_session as pty_mod
from agent_takkub.orchestrator import Orchestrator
from agent_takkub.remote import notify as notify_mod


def test_idle_tick_waits_are_polls():
    assert orch_mod._ROLE_MESSAGES_READ_BUDGET_SEC == 0.0
    assert notify_mod._TAIL_IO_WAIT_S == 0.0


def test_poll_returns_at_once_and_a_later_poll_takes_the_result():
    gate = threading.Event()
    key = ("issue-1061", time.time())

    def slow():
        gate.wait(5)
        return "records"

    started = time.perf_counter()
    assert bg_pool.call_bounded(key, slow, timeout=0.0) is None
    assert time.perf_counter() - started < 0.2
    gate.set()
    deadline = time.time() + 5
    value = None
    while value is None and time.time() < deadline:
        value = bg_pool.call_bounded(key, slow, timeout=0.0)
        time.sleep(0.01)
    assert value == "records"


def test_overage_answer_is_cached_between_ticks(monkeypatch):
    reads = []
    monkeypatch.setattr(
        "agent_takkub.limit_status.load_shared_state",
        lambda cd: reads.append(cd) or {"data": object()},
    )
    monkeypatch.setattr("agent_takkub.limit_status.is_in_overage", lambda data: True)
    monkeypatch.setattr("agent_takkub.user_profile.config_dir_for", lambda project: None)
    fake = SimpleNamespace()
    project = f"p-{time.time()}"
    assert Orchestrator._project_in_overage_cached(fake, project, 1000.0) is True
    for tick in range(1, 6):  # 25 s of 5 s ticks: no further read
        assert Orchestrator._project_in_overage_cached(fake, project, 1000.0 + 5 * tick) is True
    assert len(reads) == 1


def test_overage_refresh_never_waits_and_keeps_the_last_answer(monkeypatch):
    gate = threading.Event()

    def slow_read(cd):
        gate.wait(5)
        return {"data": object()}

    monkeypatch.setattr("agent_takkub.limit_status.load_shared_state", slow_read)
    monkeypatch.setattr("agent_takkub.limit_status.is_in_overage", lambda data: False)
    monkeypatch.setattr("agent_takkub.user_profile.config_dir_for", lambda project: None)
    project = f"p-{time.time()}"
    fake = SimpleNamespace(_overage_state_cache={project: (0.0, True)})
    started = time.perf_counter()
    assert Orchestrator._project_in_overage_cached(fake, project, 10_000.0) is True
    assert time.perf_counter() - started < 0.2
    gate.set()


def test_input_lines_render_once_per_screen_change(monkeypatch):
    session = pty_mod.PtySession.__new__(pty_mod.PtySession)
    session.screen = pty_mod._DimAwareScreen(20, 3)
    session._screen_lock = threading.Lock()
    session._output_generation = 7
    renders = []
    real = pty_mod._safe_screen_display

    def counting(screen, **kw):
        renders.append(kw)
        return real(screen, **kw)

    monkeypatch.setattr(pty_mod, "_safe_screen_display", counting)
    first = session._input_lines()
    assert session._input_lines() == first
    assert session._input_lines() == first
    assert len(renders) == 1
    session._output_generation = 8  # new PTY output
    session._input_lines()
    assert len(renders) == 2
