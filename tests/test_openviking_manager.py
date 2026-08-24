"""Tests for `agent_takkub.openviking.manager.OpenVikingManager`: the
process-lifecycle state machine (`05_PROCESS_LIFECYCLE.md`). No real
network/subprocess ever runs — `port.is_healthy`/`installer.is_installed`/
`process.OpenVikingProcess` are all stubbed."""

from __future__ import annotations

import itertools

import pytest

from agent_takkub.core.context_sources import openviking_adapter
from agent_takkub.openviking import installer, manager, port


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(openviking_adapter._ENV_URL, raising=False)
    monkeypatch.delenv(openviking_adapter._ENV_ENABLED, raising=False)
    monkeypatch.setattr(openviking_adapter, "_runtime_url", None)


class _FakeProcess:
    def __init__(self, config_path, port_num) -> None:
        self.config_path = config_path
        self.port = port_num
        self.started = False
        self.stopped = False
        self.is_alive = True

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True
        self.is_alive = False


class _FailingProcess(_FakeProcess):
    def start(self) -> None:
        raise manager.ProcessError("openviking-server exited immediately: boom")


def _monotonic_sequence(*values):
    return itertools.chain(values, itertools.repeat(values[-1]))


class TestStatus:
    def test_disabled_is_zero_cost(self, monkeypatch):
        monkeypatch.setattr(openviking_adapter, "enabled", lambda: False)
        mgr = manager.OpenVikingManager()
        result = mgr.status()
        assert result == manager.ManagerStatus(False, False, False, None, result.installed)

    def test_enabled_and_healthy(self, monkeypatch):
        monkeypatch.setattr(openviking_adapter, "enabled", lambda: True)
        monkeypatch.setattr(openviking_adapter, "base_url", lambda: "http://127.0.0.1:1933")
        monkeypatch.setattr(port, "is_healthy", lambda url, timeout=2.0: True)
        mgr = manager.OpenVikingManager()
        result = mgr.status()
        assert result.enabled is True
        assert result.healthy is True
        assert result.url == "http://127.0.0.1:1933"


class TestStartDisabledAndOverride:
    def test_start_disabled_never_picks_a_port(self, monkeypatch):
        # `installer.is_installed()` is still queried to populate the status
        # snapshot's `installed` field — only port selection/spawning must
        # be skipped entirely when disabled.
        monkeypatch.setattr(openviking_adapter, "enabled", lambda: False)
        monkeypatch.setattr(installer, "is_installed", lambda: False)
        pick_calls = []
        monkeypatch.setattr(port, "pick_port", lambda *a, **kw: pick_calls.append(1))
        mgr = manager.OpenVikingManager()
        result = mgr.start()
        assert result.enabled is False
        assert pick_calls == []

    def test_user_override_healthy_is_respected_and_never_spawns(self, monkeypatch):
        monkeypatch.setattr(openviking_adapter, "enabled", lambda: True)
        monkeypatch.setenv(openviking_adapter._ENV_URL, "http://127.0.0.1:9999")
        monkeypatch.setattr(port, "is_healthy", lambda url, timeout=2.0: True)
        spawn_called = []
        monkeypatch.setattr(manager, "OpenVikingProcess", lambda *a: spawn_called.append(a))
        mgr = manager.OpenVikingManager()

        result = mgr.start()

        assert result == manager.ManagerStatus(
            True, True, False, "http://127.0.0.1:9999", result.installed
        )
        assert spawn_called == []

    def test_user_override_unhealthy_reports_unhealthy_without_spawning(self, monkeypatch):
        monkeypatch.setattr(openviking_adapter, "enabled", lambda: True)
        monkeypatch.setenv(openviking_adapter._ENV_URL, "http://127.0.0.1:9999")
        monkeypatch.setattr(port, "is_healthy", lambda url, timeout=2.0: False)
        mgr = manager.OpenVikingManager()

        result = mgr.start()

        assert result.healthy is False
        assert result.url is None
        assert result.owned is False


class TestStartExternalHealthy:
    def test_already_alive_owned_process_reports_current_health(self, monkeypatch):
        monkeypatch.setattr(openviking_adapter, "enabled", lambda: True)
        monkeypatch.setattr(openviking_adapter, "base_url", lambda: "http://127.0.0.1:1933")
        monkeypatch.setattr(port, "is_healthy", lambda url, timeout=2.0: True)
        pick_calls = []
        monkeypatch.setattr(port, "pick_port", lambda *a, **kw: pick_calls.append(1))
        mgr = manager.OpenVikingManager()
        mgr._process = _FakeProcess(None, 1933)
        mgr._owned = True

        result = mgr.start()

        assert result == manager.ManagerStatus(True, True, True, "http://127.0.0.1:1933", True)
        assert pick_calls == []  # already-alive owned process short-circuits port selection

    def test_external_healthy_port_is_used_without_spawning(self, monkeypatch):
        monkeypatch.setattr(openviking_adapter, "enabled", lambda: True)
        monkeypatch.setattr(
            port, "pick_port", lambda *a, **kw: port.PortDecision(port=1933, already_healthy=True)
        )
        spawn_called = []
        monkeypatch.setattr(manager, "OpenVikingProcess", lambda *a: spawn_called.append(a))
        set_calls = []
        monkeypatch.setattr(
            openviking_adapter, "set_runtime_url", lambda url: set_calls.append(url)
        )
        mgr = manager.OpenVikingManager()

        result = mgr.start()

        assert result == manager.ManagerStatus(
            True, True, False, "http://127.0.0.1:1933", result.installed
        )
        assert spawn_called == []
        assert set_calls == ["http://127.0.0.1:1933"]


class TestStartSpawn:
    def _setup_common(self, monkeypatch, chosen_port=1933):
        monkeypatch.setattr(openviking_adapter, "enabled", lambda: True)
        monkeypatch.setattr(
            port,
            "pick_port",
            lambda *a, **kw: port.PortDecision(port=chosen_port, already_healthy=False),
        )
        monkeypatch.setattr(installer, "is_installed", lambda: True)
        monkeypatch.setattr(installer, "CONFIG_FILE", "ov.conf")
        monkeypatch.setattr(manager.time, "sleep", lambda s: None)

    def test_not_installed_fails_open_without_spawning(self, monkeypatch):
        monkeypatch.setattr(openviking_adapter, "enabled", lambda: True)
        monkeypatch.setattr(
            port, "pick_port", lambda *a, **kw: port.PortDecision(port=1933, already_healthy=False)
        )
        monkeypatch.setattr(installer, "is_installed", lambda: False)
        spawn_called = []
        monkeypatch.setattr(manager, "OpenVikingProcess", lambda *a: spawn_called.append(a))
        mgr = manager.OpenVikingManager()

        result = mgr.start()

        assert result.healthy is False
        assert result.installed is False
        assert result.error == "OpenViking is not installed"
        assert spawn_called == []

    def test_spawn_success_marks_owned_and_sets_runtime_url(self, monkeypatch):
        self._setup_common(monkeypatch)
        monkeypatch.setattr(manager, "OpenVikingProcess", _FakeProcess)
        monkeypatch.setattr(manager.time, "monotonic", lambda: next(_monotonic_sequence(0, 0)))
        monkeypatch.setattr(port, "is_healthy", lambda url, timeout=2.0: True)
        set_calls = []
        monkeypatch.setattr(
            openviking_adapter, "set_runtime_url", lambda url: set_calls.append(url)
        )
        mgr = manager.OpenVikingManager()

        result = mgr.start()

        assert result == manager.ManagerStatus(True, True, True, "http://127.0.0.1:1933", True)
        assert set_calls == ["http://127.0.0.1:1933"]
        assert mgr._owned is True
        assert mgr._process is not None and mgr._process.started is True

    def test_spawn_raises_process_error_fails_open(self, monkeypatch):
        self._setup_common(monkeypatch)
        monkeypatch.setattr(manager, "OpenVikingProcess", _FailingProcess)
        mgr = manager.OpenVikingManager()

        result = mgr.start()  # must not raise

        assert result.healthy is False
        assert result.owned is False
        assert "boom" in result.error
        assert mgr._process is None

    def test_spawn_health_never_arrives_stops_process_and_fails_open(self, monkeypatch):
        self._setup_common(monkeypatch)
        seq = _monotonic_sequence(0, 0, 999)
        monkeypatch.setattr(manager.time, "monotonic", lambda: next(seq))
        monkeypatch.setattr(port, "is_healthy", lambda url, timeout=2.0: False)

        created = {}

        def _factory(config_path, port_num):
            proc = _FakeProcess(config_path, port_num)
            created["proc"] = proc
            return proc

        monkeypatch.setattr(manager, "OpenVikingProcess", _factory)
        mgr = manager.OpenVikingManager()

        result = mgr.start()

        assert result.healthy is False
        assert result.owned is False
        assert created["proc"].stopped is True
        assert mgr._process is None


class TestStop:
    def test_stop_kills_only_an_owned_process(self, monkeypatch):
        set_calls = []
        monkeypatch.setattr(
            openviking_adapter, "set_runtime_url", lambda url: set_calls.append(url)
        )
        mgr = manager.OpenVikingManager()
        fake = _FakeProcess(None, 1933)
        mgr._process = fake
        mgr._owned = True

        mgr.stop()

        assert fake.stopped is True
        assert mgr._process is None
        assert mgr._owned is False
        assert set_calls == [None]

    def test_stop_never_touches_an_external_unowned_process(self, monkeypatch):
        mgr = manager.OpenVikingManager()
        fake = _FakeProcess(None, 1933)
        mgr._process = fake
        mgr._owned = False

        mgr.stop()

        assert fake.stopped is False


class TestRestart:
    def test_restart_backs_off_then_disables_after_max_attempts(self, monkeypatch):
        monkeypatch.setattr(manager.time, "sleep", lambda s: None)
        mgr = manager.OpenVikingManager()
        unhealthy = manager.ManagerStatus(True, False, False, None, True, error="still down")
        monkeypatch.setattr(mgr, "start", lambda: unhealthy)

        results = [mgr.restart() for _ in range(manager._MAX_RESTART_ATTEMPTS + 1)]

        assert mgr._disabled_for_session is True
        assert results[-1].error and "disabled for this session" in results[-1].error

    def test_restart_resets_attempt_counter_on_success(self, monkeypatch):
        monkeypatch.setattr(manager.time, "sleep", lambda s: None)
        mgr = manager.OpenVikingManager()
        healthy = manager.ManagerStatus(True, True, True, "http://127.0.0.1:1933", True)
        monkeypatch.setattr(mgr, "start", lambda: healthy)

        result = mgr.restart()

        assert result.healthy is True
        assert mgr._restart_attempts == 0
        assert mgr._disabled_for_session is False

    def test_disabled_for_session_short_circuits_further_restarts(self, monkeypatch):
        mgr = manager.OpenVikingManager()
        mgr._disabled_for_session = True
        monkeypatch.setattr(openviking_adapter, "enabled", lambda: True)
        monkeypatch.setattr(openviking_adapter, "base_url", lambda: "http://127.0.0.1:1933")
        monkeypatch.setattr(port, "is_healthy", lambda url, timeout=2.0: False)
        start_calls = []
        monkeypatch.setattr(mgr, "start", lambda: start_calls.append(1))

        mgr.restart()

        assert start_calls == []  # falls through to status(), never calls start() again
