"""Tests for `takkub ov managed status|install|start|stop|restart|doctor|
update|repair|remove|studio` (Wave 3, `10_CLI.md`). No real subprocess/
network/venv ever runs — `openviking.manager.get_manager`/`managed_runtime_
report` and `openviking.installer.run_doctor`/`update`/`uninstall` are all
stubbed, same posture `test_cli_report.py` uses for its own CLI surface.
"""

from __future__ import annotations

import pytest

from agent_takkub import cli
from agent_takkub.openviking import installer as ov_installer
from agent_takkub.openviking import manager


@pytest.fixture(autouse=True)
def _no_role_env(monkeypatch):
    monkeypatch.delenv("TAKKUB_ROLE", raising=False)
    monkeypatch.delenv("TAKKUB_PROJECT", raising=False)


@pytest.fixture(autouse=True)
def _isolate_state_file(monkeypatch, tmp_path):
    # `_cmd_ov_managed_remove` reads `installer.STATE_FILE` directly — point
    # it at a tmp path so "not installed" tests never depend on whatever's
    # actually on the machine running these tests.
    monkeypatch.setattr(ov_installer, "STATE_FILE", tmp_path / "state.json")


class _FakeManager:
    def __init__(self, start_result=None):
        self.ensure_installed_calls: list[bool] = []
        self.start_calls = 0
        self.stop_calls = 0
        self.restart_calls = 0
        self._result = start_result or manager.ManagerStatus(
            True, True, True, "http://127.0.0.1:1933", True
        )

    def ensure_installed(self, *, force=False):
        self.ensure_installed_calls.append(force)
        return True

    def start(self):
        self.start_calls += 1
        return self._result

    def stop(self):
        self.stop_calls += 1

    def restart(self):
        self.restart_calls += 1
        return self._result

    def status(self):
        return self._result


def _patch_manager(monkeypatch, fake=None):
    fake = fake or _FakeManager()
    monkeypatch.setattr(manager, "get_manager", lambda: fake)
    return fake


class TestStatus:
    def test_prints_report_fields(self, monkeypatch, capsys):
        report = manager.ManagedRuntimeReport(
            installed=True,
            version="0.4.2",
            running=True,
            owned=True,
            address="http://127.0.0.1:1933",
            healthy=True,
        )
        _patch_manager(monkeypatch)
        monkeypatch.setattr(manager, "managed_runtime_report", lambda mgr: report)

        code = cli.main(["ov", "managed", "status"])

        out = capsys.readouterr().out
        assert code == 0
        assert "installed=True version=0.4.2" in out
        assert "running=True owned=True healthy=True" in out
        assert "address=http://127.0.0.1:1933" in out

    def test_includes_error_when_present(self, monkeypatch, capsys):
        report = manager.ManagedRuntimeReport(
            installed=True,
            version=None,
            running=False,
            owned=False,
            address=None,
            healthy=False,
            error="did not respond",
        )
        _patch_manager(monkeypatch)
        monkeypatch.setattr(manager, "managed_runtime_report", lambda mgr: report)

        code = cli.main(["ov", "managed", "status"])

        assert code == 0  # status is a read-only report, never fails on its own
        assert "error=did not respond" in capsys.readouterr().out


class TestInstallRepair:
    def test_install_calls_ensure_installed_without_force(self, monkeypatch, capsys):
        fake = _patch_manager(monkeypatch)

        code = cli.main(["ov", "managed", "install"])

        assert code == 0
        assert fake.ensure_installed_calls == [False]
        assert "ok: installed" in capsys.readouterr().out

    def test_repair_calls_ensure_installed_with_force(self, monkeypatch, capsys):
        fake = _patch_manager(monkeypatch)

        code = cli.main(["ov", "managed", "repair"])

        assert code == 0
        assert fake.ensure_installed_calls == [True]
        assert "ok: repaired" in capsys.readouterr().out


class TestStartStopRestart:
    def test_start_reports_healthy(self, monkeypatch, capsys):
        fake = _patch_manager(monkeypatch)

        code = cli.main(["ov", "managed", "start"])

        assert code == 0
        assert fake.start_calls == 1
        assert "healthy=True" in capsys.readouterr().out

    def test_start_unhealthy_exits_nonzero(self, monkeypatch, capsys):
        unhealthy = manager.ManagerStatus(True, False, False, None, True, error="boom")
        _patch_manager(monkeypatch, _FakeManager(start_result=unhealthy))

        code = cli.main(["ov", "managed", "start"])

        assert code == 1
        assert "error=boom" in capsys.readouterr().out

    def test_stop_always_succeeds(self, monkeypatch, capsys):
        fake = _patch_manager(monkeypatch)

        code = cli.main(["ov", "managed", "stop"])

        assert code == 0
        assert fake.stop_calls == 1

    def test_restart_calls_restart_not_start(self, monkeypatch, capsys):
        fake = _patch_manager(monkeypatch)

        code = cli.main(["ov", "managed", "restart"])

        assert code == 0
        assert fake.restart_calls == 1
        assert fake.start_calls == 0


class TestDoctor:
    def test_doctor_prints_output(self, monkeypatch, capsys):
        _patch_manager(monkeypatch)
        monkeypatch.setattr(ov_installer, "run_doctor", lambda: (True, "all checks passed"))

        code = cli.main(["ov", "managed", "doctor"])

        assert code == 0
        assert "all checks passed" in capsys.readouterr().out

    def test_doctor_not_installed_fails(self, monkeypatch, capsys):
        _patch_manager(monkeypatch)
        monkeypatch.setattr(
            ov_installer, "run_doctor", lambda: (False, "openviking-server is not installed")
        )

        code = cli.main(["ov", "managed", "doctor"])

        assert code == 1
        assert "err:" in capsys.readouterr().out


class TestUpdate:
    def test_update_reports_versions(self, monkeypatch, capsys):
        _patch_manager(monkeypatch)
        result = ov_installer.UpdateResult(previous_version="0.4.2", new_version="0.4.9")
        monkeypatch.setattr(ov_installer, "update", lambda: result)

        code = cli.main(["ov", "managed", "update"])

        assert code == 0
        assert "updated 0.4.2 -> 0.4.9" in capsys.readouterr().out

    def test_update_includes_warning_line(self, monkeypatch, capsys):
        _patch_manager(monkeypatch)
        result = ov_installer.UpdateResult(
            previous_version="0.4.2", new_version="1.0.0", warning="outside tested range"
        )
        monkeypatch.setattr(ov_installer, "update", lambda: result)

        code = cli.main(["ov", "managed", "update"])

        assert code == 0
        assert "warning: outside tested range" in capsys.readouterr().out

    def test_update_not_installed_fails(self, monkeypatch, capsys):
        _patch_manager(monkeypatch)

        def _raise():
            raise ov_installer.InstallerError("OpenViking is not installed")

        monkeypatch.setattr(ov_installer, "update", _raise)

        code = cli.main(["ov", "managed", "update"])

        assert code == 1
        assert "OpenViking is not installed" in capsys.readouterr().out


class TestRemove:
    def test_declines_without_yes_when_input_says_no(self, monkeypatch, capsys):
        fake = _patch_manager(monkeypatch)
        monkeypatch.setattr(ov_installer, "is_installed", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        uninstall_calls = []
        monkeypatch.setattr(ov_installer, "uninstall", lambda **kw: uninstall_calls.append(kw))

        code = cli.main(["ov", "managed", "remove"])

        assert code == 1
        assert uninstall_calls == []
        assert fake.stop_calls == 0

    def test_yes_flag_skips_prompt_and_keeps_data_by_default(self, monkeypatch, capsys):
        fake = _patch_manager(monkeypatch)
        monkeypatch.setattr(ov_installer, "is_installed", lambda: True)

        def _fail_input(prompt=""):
            raise AssertionError("must not prompt when --yes is passed")

        monkeypatch.setattr("builtins.input", _fail_input)
        uninstall_calls = []
        monkeypatch.setattr(ov_installer, "uninstall", lambda **kw: uninstall_calls.append(kw))

        code = cli.main(["ov", "managed", "remove", "--yes"])

        assert code == 0
        assert fake.stop_calls == 1
        assert uninstall_calls == [{"remove_data": False}]
        assert "config/data kept" in capsys.readouterr().out

    def test_purge_data_forwards_remove_data_true(self, monkeypatch, capsys):
        _patch_manager(monkeypatch)
        monkeypatch.setattr(ov_installer, "is_installed", lambda: True)
        uninstall_calls = []
        monkeypatch.setattr(ov_installer, "uninstall", lambda **kw: uninstall_calls.append(kw))

        code = cli.main(["ov", "managed", "remove", "--yes", "--purge-data"])

        assert code == 0
        assert uninstall_calls == [{"remove_data": True}]
        assert "data purged" in capsys.readouterr().out

    def test_not_installed_fails_without_prompting(self, monkeypatch, capsys):
        _patch_manager(monkeypatch)
        monkeypatch.setattr(ov_installer, "is_installed", lambda: False)

        def _fail_input(prompt=""):
            raise AssertionError("must not prompt when nothing is installed")

        monkeypatch.setattr("builtins.input", _fail_input)

        code = cli.main(["ov", "managed", "remove"])

        assert code == 1
        assert "not installed" in capsys.readouterr().out


class TestStudio:
    def test_opens_browser_when_healthy(self, monkeypatch, capsys):
        healthy = manager.ManagerStatus(True, True, True, "http://127.0.0.1:1933", True)
        _patch_manager(monkeypatch, _FakeManager(start_result=healthy))
        opened = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

        code = cli.main(["ov", "managed", "studio"])

        assert code == 0
        assert opened == ["http://127.0.0.1:1933/studio"]

    def test_not_running_fails_without_opening_browser(self, monkeypatch, capsys):
        unhealthy = manager.ManagerStatus(True, False, False, None, True)
        _patch_manager(monkeypatch, _FakeManager(start_result=unhealthy))
        opened = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

        code = cli.main(["ov", "managed", "studio"])

        assert code == 1
        assert opened == []
        assert "start it first" in capsys.readouterr().out
