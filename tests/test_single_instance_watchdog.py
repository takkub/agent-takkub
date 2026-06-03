"""Unit tests for single-instance guard and dead-man watchdog (issue #34).

No real Qt windows are opened and no real watchdog threads are spawned.
Tests verify the underlying logic only.

Import order is intentional: agent_takkub modules are imported at the *module*
level so QtWebEngineWidgets is loaded before the qapp fixture creates a
QCoreApplication (the engine requires this ordering).
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QLockFile

# Must be module-level so QtWebEngineWidgets is imported before QCoreApplication.
import agent_takkub.app as app_mod
import agent_takkub.main_window as mw_mod

# ─────────────────────────────────────────────────────────────
# QCoreApplication fixture
# ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


# ─────────────────────────────────────────────────────────────
# 1. Single-instance guard — QLockFile behaviour
# ─────────────────────────────────────────────────────────────


class TestSingleInstanceGuard:
    """QLockFile prevents two cockpit processes from opening simultaneously."""

    def test_first_lock_succeeds(self, qapp: QCoreApplication, tmp_path: Path) -> None:
        lock = QLockFile(str(tmp_path / "cockpit.lock"))
        assert lock.tryLock(100), "First tryLock must succeed"
        lock.unlock()

    def test_second_lock_fails_while_first_held(
        self, qapp: QCoreApplication, tmp_path: Path
    ) -> None:
        lock1 = QLockFile(str(tmp_path / "cockpit.lock"))
        lock2 = QLockFile(str(tmp_path / "cockpit.lock"))
        assert lock1.tryLock(100)
        assert not lock2.tryLock(50), "Second tryLock must fail while first is held"
        lock1.unlock()

    def test_lock_available_again_after_unlock(
        self, qapp: QCoreApplication, tmp_path: Path
    ) -> None:
        lock1 = QLockFile(str(tmp_path / "cockpit.lock"))
        lock2 = QLockFile(str(tmp_path / "cockpit.lock"))
        lock1.tryLock(100)
        lock1.unlock()
        assert lock2.tryLock(100), "Lock must be acquirable after unlock"
        lock2.unlock()

    def test_lock_released_on_object_destruction(
        self, qapp: QCoreApplication, tmp_path: Path
    ) -> None:
        """Lock must be released when the QLockFile object is garbage-collected."""
        lock_path = str(tmp_path / "cockpit.lock")
        lock1 = QLockFile(lock_path)
        lock1.tryLock(100)
        del lock1  # triggers destructor → unlock

        lock2 = QLockFile(lock_path)
        assert lock2.tryLock(100), "Lock must be acquirable after GC of previous holder"
        lock2.unlock()


# ─────────────────────────────────────────────────────────────
# 2. Dead-man watchdog — _watchdog_should_exit helper
# ─────────────────────────────────────────────────────────────


class TestWatchdogShouldExit:
    """_watchdog_should_exit is a pure function — no threading needed."""

    def _fn(self, heartbeat_ts: float, now: float, timeout_s: float) -> bool:
        return app_mod._watchdog_should_exit(heartbeat_ts, now, timeout_s)

    def test_fresh_heartbeat_does_not_trigger(self) -> None:
        now = 1_000_000.0
        heartbeat = now - 5.0  # 5 s ago — well within 30 s threshold
        assert not self._fn(heartbeat, now, 30.0)

    def test_stale_heartbeat_triggers(self) -> None:
        now = 1_000_000.0
        heartbeat = now - 31.0  # 31 s ago — past threshold
        assert self._fn(heartbeat, now, 30.0)

    def test_exactly_at_threshold_does_not_trigger(self) -> None:
        now = 1_000_000.0
        heartbeat = now - 30.0  # exactly at threshold — NOT triggered (strictly >)
        assert not self._fn(heartbeat, now, 30.0)

    def test_custom_timeout(self) -> None:
        now = 1_000_000.0
        assert self._fn(now - 11.0, now, 10.0)
        assert not self._fn(now - 9.0, now, 10.0)


# ─────────────────────────────────────────────────────────────
# 3. Dead-man watchdog — thread calls os._exit when heartbeat stalls
# ─────────────────────────────────────────────────────────────


class TestWatchdogThreadBehaviour:
    """Verify that _start_deadman_watchdog fires os._exit on a stale heartbeat.

    We use a very short timeout (0.05 s) and poll (0.01 s) so tests finish fast.
    """

    def test_watchdog_calls_os_exit_when_heartbeat_stale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(app_mod, "_WATCHDOG_TIMEOUT_S", 0.05)
        monkeypatch.setattr(app_mod, "_WATCHDOG_POLL_S", 0.01)

        fired = threading.Event()
        exit_called: list[int] = []
        stop = threading.Event()

        def _fake_exit(code: int) -> None:
            if not fired.is_set():
                exit_called.append(code)
                fired.set()

        window = MagicMock()
        window._heartbeat_ts = time.monotonic() - 1.0  # frozen 1 s in the past

        with patch.object(os, "_exit", side_effect=_fake_exit):
            app_mod._start_deadman_watchdog(window, _stop=stop)
            fired.wait(timeout=2.0)
            stop.set()  # cleanly halt daemon before os._exit is un-patched

        assert exit_called == [1], "Watchdog must call os._exit(1) on stale heartbeat"

    def test_watchdog_does_not_fire_with_live_heartbeat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(app_mod, "_WATCHDOG_TIMEOUT_S", 0.1)
        monkeypatch.setattr(app_mod, "_WATCHDOG_POLL_S", 0.02)

        exit_called: list[int] = []
        stop = threading.Event()

        def _fake_exit(code: int) -> None:
            exit_called.append(code)

        window = MagicMock()
        window._heartbeat_ts = time.monotonic()

        keepalive_stop = threading.Event()

        def _keep_alive() -> None:
            while not keepalive_stop.is_set():
                window._heartbeat_ts = time.monotonic()
                time.sleep(0.01)

        keeper = threading.Thread(target=_keep_alive, daemon=True)
        keeper.start()

        with patch.object(os, "_exit", side_effect=_fake_exit):
            app_mod._start_deadman_watchdog(window, _stop=stop)
            time.sleep(0.3)  # 3× the timeout — watchdog must not fire
            stop.set()  # halt daemon before os._exit is un-patched

        keepalive_stop.set()
        assert exit_called == [], "Watchdog must NOT fire while heartbeat advances"


# ─────────────────────────────────────────────────────────────
# 4. MainWindow heartbeat attribute
# ─────────────────────────────────────────────────────────────


class TestMainWindowHeartbeat:
    """_tick_heartbeat advances _heartbeat_ts without needing a real Qt window."""

    def _stub_window(self):
        with patch.object(mw_mod.MainWindow, "__init__", lambda self: None):
            return mw_mod.MainWindow.__new__(mw_mod.MainWindow)

    def test_tick_heartbeat_updates_ts(self) -> None:
        win = self._stub_window()
        win._heartbeat_ts = time.monotonic() - 5.0  # simulate stale timestamp
        before = win._heartbeat_ts
        mw_mod.MainWindow._tick_heartbeat(win)
        assert win._heartbeat_ts > before, "_tick_heartbeat must advance _heartbeat_ts"

    def test_tick_heartbeat_ts_is_recent(self) -> None:
        win = self._stub_window()
        win._heartbeat_ts = 0.0
        mw_mod.MainWindow._tick_heartbeat(win)
        age = time.monotonic() - win._heartbeat_ts
        assert age < 1.0, "_tick_heartbeat must set _heartbeat_ts to a recent monotonic value"
