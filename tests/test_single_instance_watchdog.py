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
# 5. Per-DATA_HOME instance lock key (isolation plan, finding C1)
# ─────────────────────────────────────────────────────────────


class TestInstanceLockKey:
    """dev and prod (different DATA_HOME) must never collide on the same
    single-instance lock file — each gets a stable key derived from its own
    DATA_HOME."""

    def test_same_data_home_yields_same_key(self, tmp_path: Path) -> None:
        home = tmp_path / "agent-takkub-home"
        assert app_mod._instance_lock_key(home) == app_mod._instance_lock_key(home)

    def test_different_data_home_yields_different_key(self, tmp_path: Path) -> None:
        a = tmp_path / "dev-checkout"
        b = tmp_path / "installed-home"
        assert app_mod._instance_lock_key(a) != app_mod._instance_lock_key(b)

    def test_key_is_short_hex(self, tmp_path: Path) -> None:
        key = app_mod._instance_lock_key(tmp_path / "home")
        assert len(key) == 12
        int(key, 16)  # raises ValueError if not hex

    def test_defaults_to_module_data_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(app_mod, "DATA_HOME", tmp_path / "some-home")
        assert app_mod._instance_lock_key() == app_mod._instance_lock_key(tmp_path / "some-home")

    def test_restart_successor_computes_identical_key(self, tmp_path: Path) -> None:
        """The restart-successor process inherits the SAME env (hence the
        same resolved DATA_HOME) as its predecessor, so it must land on the
        identical lock key/path — this is what lets _wait_predecessor_exit
        actually wait on the right lock."""
        home = tmp_path / "same-data-home"
        predecessor_key = app_mod._instance_lock_key(home)
        successor_key = app_mod._instance_lock_key(home)
        assert predecessor_key == successor_key


# ─────────────────────────────────────────────────────────────
# 6. Startup audit breadcrumb (isolation plan, finding C2)
# ─────────────────────────────────────────────────────────────


class TestLogInstanceBoot:
    def test_logs_dev_mode_with_full_identity(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import agent_takkub.config as config_mod

        monkeypatch.setattr(config_mod, "DATA_HOME", tmp_path)
        monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(config_mod, "SETTINGS_HOME", tmp_path / "settings")
        monkeypatch.setattr(config_mod, "ASSETS_ROOT", tmp_path)
        monkeypatch.setattr(config_mod, "CLI_BIN_DIR", tmp_path / "bin")

        logged: list[dict] = []
        with patch(
            "agent_takkub.orchestrator._log_event",
            side_effect=lambda event, **kw: logged.append({"event": event, **kw}),
        ):
            app_mod._log_instance_boot()

        assert len(logged) == 1
        rec = logged[0]
        assert rec["event"] == "instance_boot"
        assert rec["mode"] == "dev"
        assert rec["data_home"] == str(tmp_path)
        assert rec["cli_bin_dir"] == str(tmp_path / "bin")
        assert "lock_path" in rec and "port_file" in rec

    def test_logs_installed_mode_when_data_home_differs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import agent_takkub.config as config_mod

        monkeypatch.setattr(config_mod, "DATA_HOME", tmp_path / "data-home")
        monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path / "venv-lib")

        logged: list[dict] = []
        with patch(
            "agent_takkub.orchestrator._log_event",
            side_effect=lambda event, **kw: logged.append({"event": event, **kw}),
        ):
            app_mod._log_instance_boot()

        assert logged[0]["mode"] == "installed"

    def test_never_raises_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with patch("agent_takkub.orchestrator._log_event", side_effect=RuntimeError("boom")):
            app_mod._log_instance_boot()  # must not raise


# ─────────────────────────────────────────────────────────────
# 7. QtWebEngine per-instance cache/storage path (isolation plan, finding C4)
# ─────────────────────────────────────────────────────────────


class TestConfigureWebengineProfile:
    def test_sets_storage_and_cache_under_runtime_dir(
        self, qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import agent_takkub.config as config_mod

        monkeypatch.setattr(config_mod, "RUNTIME_DIR", tmp_path / "runtime")

        fake_profile = MagicMock()
        fake_cls = MagicMock()
        fake_cls.defaultProfile.return_value = fake_profile
        with patch("PyQt6.QtWebEngineCore.QWebEngineProfile", fake_cls):
            app_mod._configure_webengine_profile()

        fake_profile.setPersistentStoragePath.assert_called_once_with(
            str(tmp_path / "runtime" / "webengine" / "storage")
        )
        fake_profile.setCachePath.assert_called_once_with(
            str(tmp_path / "runtime" / "webengine" / "cache")
        )

    def test_never_raises_on_failure(self) -> None:
        fake_cls = MagicMock()
        fake_cls.defaultProfile.side_effect = RuntimeError("boom")
        with patch("PyQt6.QtWebEngineCore.QWebEngineProfile", fake_cls):
            app_mod._configure_webengine_profile()  # must not raise


# ─────────────────────────────────────────────────────────────
# 8. First-boot prod Claude profile bootstrap (isolation plan, finding C5)
# ─────────────────────────────────────────────────────────────


class TestBootstrapProdClaudeProfile:
    def test_logs_event_when_a_clone_happens(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import agent_takkub.user_profile as up_mod

        monkeypatch.setattr(up_mod, "bootstrap_default_profile", lambda: True)
        monkeypatch.setattr(up_mod, "_DEFAULT_CONFIG_DIR", Path("/fake/claude-config"))

        logged: list[dict] = []
        with patch(
            "agent_takkub.orchestrator._log_event",
            side_effect=lambda event, **kw: logged.append({"event": event, **kw}),
        ):
            app_mod._bootstrap_prod_claude_profile()

        assert len(logged) == 1
        assert logged[0]["event"] == "prod_claude_profile_bootstrapped"
        assert logged[0]["dest"] == str(Path("/fake/claude-config"))

    def test_no_log_when_nothing_cloned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import agent_takkub.user_profile as up_mod

        monkeypatch.setattr(up_mod, "bootstrap_default_profile", lambda: False)

        logged: list[dict] = []
        with patch(
            "agent_takkub.orchestrator._log_event",
            side_effect=lambda event, **kw: logged.append({"event": event, **kw}),
        ):
            app_mod._bootstrap_prod_claude_profile()

        assert logged == []

    def test_never_raises_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import agent_takkub.user_profile as up_mod

        monkeypatch.setattr(
            up_mod, "bootstrap_default_profile", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        app_mod._bootstrap_prod_claude_profile()  # must not raise


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


class TestWaitPredecessorExit:
    """Restart-successor lock wait (no auto-kill, no 'already running' dialog)."""

    class _Lock:
        def __init__(self, free_after_tries: int):
            self.free_after = free_after_tries
            self.tries = 0

        def tryLock(self, _ms):
            self.tries += 1
            return self.tries >= self.free_after

    def test_acquires_once_predecessor_exits(self):
        from agent_takkub.app import _wait_predecessor_exit

        clock = {"t": 0.0}

        def fake_sleep(s):
            clock["t"] += s

        lock = self._Lock(free_after_tries=4)  # frees after ~1s of polling
        assert (
            _wait_predecessor_exit(
                lock, timeout_s=20.0, poll_s=0.25, _sleep=fake_sleep, _clock=lambda: clock["t"]
            )
            is True
        )
        assert lock.tries == 4

    def test_times_out_when_lock_never_frees(self):
        from agent_takkub.app import _wait_predecessor_exit

        clock = {"t": 0.0}

        def fake_sleep(s):
            clock["t"] += s

        lock = self._Lock(free_after_tries=10**9)
        assert (
            _wait_predecessor_exit(
                lock, timeout_s=2.0, poll_s=0.25, _sleep=fake_sleep, _clock=lambda: clock["t"]
            )
            is False
        )
        # bounded: ~timeout/poll attempts, not unbounded
        assert lock.tries <= 9
