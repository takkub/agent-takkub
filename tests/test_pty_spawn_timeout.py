"""Tests for the native pty-constructor timeout bound (issue #139).

A wedged pywinpty/ptyprocess spawn() call once blocked the Qt main thread —
and the spawn-in-progress FIFO arbiter behind it — for 47+ minutes (3,412,178
ms in events.log) because neither backend bounds its own blocking native
constructor call. ``spawn_pty_bounded`` races that call against a wall-clock
timeout on a worker thread so a caller gets control back instead of hanging
forever, and tears down whatever the worker eventually produces if the caller
already gave up. ``PtySession.spawn`` uses it for the real native call — see
test_pty_session_spawn_timeout.py for that integration.
"""

from __future__ import annotations

import threading

import pytest

import agent_takkub._pty_backend as backend
from agent_takkub._pty_backend import PtySpawnTimeout, spawn_pty_bounded


def test_spawn_pty_bounded_returns_proc_on_fast_success(monkeypatch) -> None:
    sentinel = object()
    monkeypatch.setattr(backend, "spawn_pty", lambda *a, **kw: sentinel)

    proc = spawn_pty_bounded(["echo"], cwd=None, env=None, rows=24, cols=80, timeout_sec=1.0)
    assert proc is sentinel


def test_spawn_pty_bounded_propagates_native_exception(monkeypatch) -> None:
    def _boom(*a, **kw):
        raise FileNotFoundError("no such binary")

    monkeypatch.setattr(backend, "spawn_pty", _boom)

    with pytest.raises(FileNotFoundError):
        spawn_pty_bounded(["nope"], cwd=None, env=None, rows=24, cols=80, timeout_sec=1.0)


def test_spawn_pty_bounded_raises_timeout_without_blocking_caller(monkeypatch) -> None:
    release = threading.Event()

    def _slow(*a, **kw):
        release.wait(10)  # simulates a wedged native call
        return object()

    monkeypatch.setattr(backend, "spawn_pty", _slow)

    with pytest.raises(PtySpawnTimeout):
        spawn_pty_bounded(["slow"], cwd=None, env=None, rows=24, cols=80, timeout_sec=0.2)
    # Raised while the (simulated) native call is still wedged on the gate —
    # proves it's bounded by the timeout, not the caller waiting it out (#139).
    assert not release.is_set()

    release.set()  # let the abandoned worker thread finish instead of leaking


def test_spawn_pty_bounded_terminates_late_result_after_abandon(monkeypatch) -> None:
    """When the native call finally returns AFTER the caller already gave up
    on it, the abandoned process must be force-terminated, not leaked (#139:
    'จัดการ handle/thread ให้เรียบร้อย' — releasing the flag alone would leave
    a ghost process nobody manages)."""
    release = threading.Event()
    terminated = threading.Event()

    class _FakeProc:
        def terminate(self, force: bool = False) -> None:
            assert force is True
            terminated.set()

    def _slow(*a, **kw):
        release.wait(10)
        return _FakeProc()

    monkeypatch.setattr(backend, "spawn_pty", _slow)

    with pytest.raises(PtySpawnTimeout):
        spawn_pty_bounded(["slow"], cwd=None, env=None, rows=24, cols=80, timeout_sec=0.1)

    release.set()
    assert terminated.wait(5.0), "abandoned process was never terminated"


def test_spawn_pty_bounded_terminate_failure_is_swallowed(monkeypatch) -> None:
    """A late, abandoned proc whose own terminate() raises must not crash the
    background worker thread (best-effort cleanup, same contract as every
    other terminate()-in-a-finally in this codebase)."""
    release = threading.Event()
    tried = threading.Event()

    class _BrokenProc:
        def terminate(self, force: bool = False) -> None:
            tried.set()
            raise RuntimeError("already dead")

    def _slow(*a, **kw):
        release.wait(10)
        return _BrokenProc()

    monkeypatch.setattr(backend, "spawn_pty", _slow)

    with pytest.raises(PtySpawnTimeout):
        spawn_pty_bounded(["slow"], cwd=None, env=None, rows=24, cols=80, timeout_sec=0.1)

    release.set()
    assert tried.wait(5.0)
