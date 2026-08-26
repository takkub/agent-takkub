"""#406 — cockpit-owned mb Chrome is released once the last pane that can use
it is gone, instead of surviving (≈500 MB, about:blank) until app shutdown.

Drives the orchestrator methods unbound against small fakes (same technique
as test_orchestrator_ram_status.py); the one test that needs a real QTimer
parents it to a throwaway QObject and stops it before returning so the
conftest QTimer-leak tracker stays clean."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from PyQt6.QtCore import QObject

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator


class _Pane:
    def __init__(self, *, alive: bool = True, state: str = "working") -> None:
        self.session = object() if alive else None
        self.state = state


class _Manager:
    def __init__(self) -> None:
        self.closed = threading.Event()

    def close(self) -> None:
        self.closed.set()


def _fake(panes: dict, *, manager=None) -> SimpleNamespace:
    fake = SimpleNamespace(_panes_by_project={"proj": panes}, _native_chrome=manager)
    fake._native_chrome_in_use = lambda: Orchestrator._native_chrome_in_use(fake)
    return fake


def test_in_use_only_counts_live_non_sharded_browser_roles(monkeypatch) -> None:
    monkeypatch.setattr(orch_mod.sys, "platform", "win32", raising=False)
    import agent_takkub.browser_chrome as bc

    monkeypatch.setattr(bc.sys, "platform", "win32")

    assert Orchestrator._native_chrome_in_use(_fake({"qa": _Pane()}))
    assert Orchestrator._native_chrome_in_use(_fake({"critic": _Pane()}))
    # shards never share mb Chrome (#92) → they don't hold it open either
    assert not Orchestrator._native_chrome_in_use(_fake({"qa#2": _Pane()}))
    # non-browser roles
    assert not Orchestrator._native_chrome_in_use(_fake({"backend": _Pane(), "lead": _Pane()}))
    # a browser pane whose session is gone / already exited doesn't count
    assert not Orchestrator._native_chrome_in_use(_fake({"qa": _Pane(alive=False)}))
    assert not Orchestrator._native_chrome_in_use(_fake({"qa": _Pane(state="exited")}))


def test_release_closes_manager_off_thread_when_idle(monkeypatch) -> None:
    import agent_takkub.browser_chrome as bc

    monkeypatch.setattr(bc.sys, "platform", "win32")
    manager = _Manager()
    fake = _fake({"backend": _Pane()}, manager=manager)
    events: list[str] = []
    monkeypatch.setattr(orch_mod, "_log_event", lambda name, **kw: events.append(name))

    Orchestrator._release_native_chrome_if_idle(fake)

    assert manager.closed.wait(timeout=5), "close() must run (on a worker thread)"
    assert events == ["native_chrome_idle_release"]


def test_release_is_a_noop_while_a_browser_pane_is_alive(monkeypatch) -> None:
    import agent_takkub.browser_chrome as bc

    monkeypatch.setattr(bc.sys, "platform", "win32")
    manager = _Manager()
    fake = _fake({"qa": _Pane()}, manager=manager)

    Orchestrator._release_native_chrome_if_idle(fake)

    assert not manager.closed.wait(timeout=0.2)


def test_release_is_a_noop_without_a_manager() -> None:
    fake = _fake({}, manager=None)
    Orchestrator._release_native_chrome_if_idle(fake)  # must not raise
    Orchestrator._schedule_native_chrome_idle_release(fake)  # no QTimer created
    assert not hasattr(fake, "_native_chrome_idle_timer")


def test_schedule_arms_a_single_shot_grace_timer(monkeypatch) -> None:
    monkeypatch.setattr(orch_mod, "_NATIVE_CHROME_IDLE_GRACE_MS", 60_000)

    class _Host(QObject):
        pass

    host = _Host()
    host._panes_by_project = {"proj": {}}
    host._native_chrome = _Manager()
    host._release_native_chrome_if_idle = lambda: None
    try:
        Orchestrator._schedule_native_chrome_idle_release(host)
        timer = host._native_chrome_idle_timer
        assert timer.isActive()
        assert timer.isSingleShot()
        assert timer.interval() == 60_000
        # re-scheduling restarts the same timer rather than stacking a new one
        Orchestrator._schedule_native_chrome_idle_release(host)
        assert host._native_chrome_idle_timer is timer
    finally:
        host._native_chrome_idle_timer.stop()
        host.deleteLater()
