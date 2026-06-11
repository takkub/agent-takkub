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
# 3. Dead-man watchdog — DIAGNOSTIC ONLY (hard-kill disabled 2026-06-10)
# ─────────────────────────────────────────────────────────────


class TestWatchdogThreadBehaviour:
    """Verify _start_deadman_watchdog logs a wedge but NEVER kills the process.

    Hard-kill was removed at the user's request (it was nuking the cockpit on
    resume-from-sleep and on transient native wedges). The daemon must still
    dump the wedged main-thread stack for diagnostics, but must not call
    os._exit. We use a very short timeout (0.05 s) and poll (0.01 s) so tests
    finish fast.
    """

    def test_watchdog_does_not_kill_on_stale_heartbeat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale heartbeat (a wedge) must NOT call os._exit — the cockpit
        stays alive even when wedged; the user closes it manually if needed."""
        monkeypatch.setattr(app_mod, "_WATCHDOG_TIMEOUT_S", 0.05)
        monkeypatch.setattr(app_mod, "_WATCHDOG_SOFT_STALL_S", 999.0)  # isolate hard branch
        monkeypatch.setattr(app_mod, "_WATCHDOG_POLL_S", 0.01)
        monkeypatch.setattr(app_mod, "_BOOT_LOG_FH", None)

        dump_calls: list[str] = []
        monkeypatch.setattr(app_mod, "_dump_main_stack", lambda header: dump_calls.append(header))

        exit_called: list[int] = []
        stop = threading.Event()
        window = MagicMock()
        window._heartbeat_ts = time.monotonic() - 1.0  # frozen 1 s in the past

        with patch.object(os, "_exit", side_effect=lambda c: exit_called.append(c)):
            app_mod._start_deadman_watchdog(window, _stop=stop)
            time.sleep(0.15)  # several poll cycles past the hard threshold
            stop.set()

        assert exit_called == [], "watchdog must NEVER call os._exit (hard-kill disabled)"
        assert dump_calls, "a wedge must still dump the main-thread stack for diagnostics"
        assert "wedge" in dump_calls[0]

    def test_watchdog_dumps_main_thread_stack_on_wedge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On a wedge the watchdog must still dump the main-thread stack
        (faulthandler all-threads) to boot.log — the diagnostic survives the
        hard-kill removal so freezes stay debuggable."""
        monkeypatch.setattr(app_mod, "_WATCHDOG_TIMEOUT_S", 0.05)
        monkeypatch.setattr(app_mod, "_WATCHDOG_SOFT_STALL_S", 999.0)  # isolate hard branch
        monkeypatch.setattr(app_mod, "_WATCHDOG_POLL_S", 0.01)
        monkeypatch.setattr(app_mod, "_BOOT_LOG_FH", MagicMock())

        dump_calls: list[tuple] = []
        monkeypatch.setattr(
            app_mod.faulthandler,
            "dump_traceback",
            lambda *a, **k: dump_calls.append((a, k)),
        )

        exit_called: list[int] = []
        stop = threading.Event()
        window = MagicMock()
        window._heartbeat_ts = time.monotonic() - 1.0  # frozen 1 s in the past

        with patch.object(os, "_exit", side_effect=lambda c: exit_called.append(c)):
            app_mod._start_deadman_watchdog(window, _stop=stop)
            time.sleep(0.15)
            stop.set()

        assert exit_called == [], "hard-kill disabled: os._exit must not be called"
        assert dump_calls, "faulthandler.dump_traceback must run on a wedge"
        # Dump must include every thread.
        assert dump_calls[0][1].get("all_threads") is True

    def test_watchdog_soft_stall_dumps_without_exiting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transient stall (past the soft threshold but below the hard kill)
        must dump the main-thread stack WITHOUT calling os._exit — this is how
        the sub-30 s spawn freeze gets a diagnosable stack."""
        monkeypatch.setattr(app_mod, "_WATCHDOG_TIMEOUT_S", 999.0)  # never hard-kill
        monkeypatch.setattr(app_mod, "_WATCHDOG_SOFT_STALL_S", 0.05)
        monkeypatch.setattr(app_mod, "_WATCHDOG_POLL_S", 0.01)
        monkeypatch.setattr(app_mod, "_BOOT_LOG_FH", MagicMock())

        dump_calls: list[str] = []
        monkeypatch.setattr(app_mod, "_dump_main_stack", lambda header: dump_calls.append(header))

        exit_called: list[int] = []
        stop = threading.Event()
        window = MagicMock()
        window._heartbeat_ts = time.monotonic() - 1.0  # 1 s stale → past soft, below hard

        with patch.object(os, "_exit", side_effect=lambda c: exit_called.append(c)):
            app_mod._start_deadman_watchdog(window, _stop=stop)
            time.sleep(0.15)
            stop.set()

        assert exit_called == [], "soft stall must NOT kill the process"
        assert dump_calls, "soft stall must dump the main-thread stack"
        assert "SOFT stall" in dump_calls[0]

    def test_watchdog_does_not_fire_with_live_heartbeat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(app_mod, "_WATCHDOG_TIMEOUT_S", 0.1)
        monkeypatch.setattr(app_mod, "_WATCHDOG_POLL_S", 0.02)
        monkeypatch.setattr(app_mod, "_WATCHDOG_SOFT_STALL_S", 999.0)  # never soft-dump here
        monkeypatch.setattr(app_mod, "_BOOT_LOG_FH", None)

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
# 3b. Sub-hard-kill stall tracker — _StallTracker (pure logic)
# ─────────────────────────────────────────────────────────────


class TestStallTracker:
    """_StallTracker turns a stream of (age, spawn_in_progress) samples into one
    logged record per stall episode, capturing peak duration + spawn latch."""

    def _t(self, threshold: float = 0.75) -> app_mod._StallTracker:
        return app_mod._StallTracker(threshold)

    def test_below_threshold_never_records(self) -> None:
        t = self._t()
        for age in (0.0, 0.1, 0.5, 0.74):
            assert t.update(age, False) is None

    def test_active_episode_returns_none_until_recovery(self) -> None:
        t = self._t()
        assert t.update(1.0, False) is None  # crosses threshold — episode open
        assert t.update(1.5, False) is None  # still stalled
        rec = t.update(0.2, False)  # recovered → emit once
        assert rec is not None
        assert rec["duration_ms"] == 1500  # peak, not the recovery sample
        assert rec["spawn_in_progress"] is False

    def test_peak_is_max_over_episode(self) -> None:
        t = self._t()
        t.update(0.9, False)
        t.update(2.3, False)
        t.update(1.1, False)
        rec = t.update(0.0, False)
        assert rec["duration_ms"] == 2300

    def test_spawn_latches_even_if_only_briefly_true(self) -> None:
        # spawn finishes mid-episode (True on one sample, False on others) — the
        # record must still attribute the stall to a spawn.
        t = self._t()
        t.update(1.0, False)
        t.update(1.2, True)  # spawn seen here
        t.update(1.4, False)
        rec = t.update(0.1, False)
        assert rec["spawn_in_progress"] is True

    def test_no_spawn_episode_reports_false(self) -> None:
        t = self._t()
        t.update(1.0, False)
        rec = t.update(0.0, False)
        assert rec["spawn_in_progress"] is False

    def test_separate_episodes_emit_separate_records(self) -> None:
        t = self._t()
        t.update(1.0, True)
        rec1 = t.update(0.1, False)
        assert rec1 is not None and rec1["spawn_in_progress"] is True
        # second episode starts fresh — peak + spawn latch reset
        assert t.update(0.2, False) is None  # below threshold, no double-emit
        t.update(0.9, False)
        rec2 = t.update(0.0, False)
        assert rec2 is not None
        assert rec2["duration_ms"] == 900
        assert rec2["spawn_in_progress"] is False


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
