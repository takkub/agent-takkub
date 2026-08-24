"""Tests for `agent_takkub.openviking.installer`: managed-venv install flow.

No real `pip install` runs here — every `subprocess.run` call is stubbed via
`installer._run`, mirroring how `test_remote_tunnel.py` stubs
`tunnel.subprocess.Popen` rather than spawning anything real.
"""

from __future__ import annotations

import subprocess

import pytest

from agent_takkub.openviking import installer


@pytest.fixture(autouse=True)
def _isolate_home(monkeypatch, tmp_path):
    home = tmp_path / "openviking"
    monkeypatch.setattr(installer, "OPENVIKING_HOME", home)
    monkeypatch.setattr(installer, "VENV_DIR", home / "venv")
    monkeypatch.setattr(installer, "CONFIG_DIR", home / "config")
    monkeypatch.setattr(installer, "CONFIG_FILE", home / "config" / "ov.conf")
    monkeypatch.setattr(installer, "STATE_FILE", home / "state.json")
    monkeypatch.setattr(installer, "DATA_DIR", home / "data")


def _completed(returncode=0, stdout=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout)


class TestServerExecutablePaths:
    def test_windows_executable_path(self, monkeypatch):
        monkeypatch.setattr(installer.sys, "platform", "win32")
        exe = installer.server_executable()
        assert exe == installer.VENV_DIR / "Scripts" / "openviking-server.exe"

    def test_posix_executable_path(self, monkeypatch):
        monkeypatch.setattr(installer.sys, "platform", "linux")
        exe = installer.server_executable()
        assert exe == installer.VENV_DIR / "bin" / "openviking-server"


class TestIsInstalled:
    def test_false_when_executable_missing(self):
        assert installer.is_installed() is False

    def test_true_when_executable_present(self, monkeypatch):
        exe = installer.server_executable()
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("", encoding="utf-8")
        assert installer.is_installed() is True


class TestEnsureInstalled:
    def test_skips_all_work_when_already_installed(self, monkeypatch):
        exe = installer.server_executable()
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("", encoding="utf-8")
        calls = []
        monkeypatch.setattr(installer, "_run", lambda *a, **kw: calls.append(a) or _completed())

        assert installer.ensure_installed() is True
        assert calls == []

    def test_force_reinstalls_even_when_already_installed(self, monkeypatch):
        exe = installer.server_executable()
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("", encoding="utf-8")
        calls = []
        monkeypatch.setattr(
            installer, "_run", lambda argv, **kw: calls.append(argv) or _completed()
        )

        installer.ensure_installed(force=True)

        assert len(calls) == 4  # venv create, pip install, doctor verify, version probe

    def test_runs_venv_pip_verify_and_writes_state(self, monkeypatch):
        exe = installer.server_executable()

        def _fake_run(argv, **kw):
            if "pip" in argv and "install" in argv:
                # A real `pip install openviking` would drop the console
                # script into venv/Scripts (or bin) at this point.
                exe.parent.mkdir(parents=True, exist_ok=True)
                exe.write_text("", encoding="utf-8")
                return _completed()
            if argv[-1] == "doctor":
                return _completed(returncode=1)  # doctor's own exit code is not load-bearing
            if "-c" in argv:
                return _completed(stdout="0.4.2\n")
            return _completed()

        monkeypatch.setattr(installer, "_run", _fake_run)

        assert installer.ensure_installed() is True
        assert installer.STATE_FILE.exists()
        state = installer.read_state()
        assert state["version"] == "0.4.2"
        assert "installed_at" in state

    def test_venv_creation_failure_raises(self, monkeypatch):
        monkeypatch.setattr(
            installer, "_run", lambda argv, **kw: _completed(returncode=1, stdout="boom")
        )
        with pytest.raises(installer.InstallerError, match="venv"):
            installer.ensure_installed()

    def test_pip_install_failure_raises(self, monkeypatch):
        def _fake_run(argv, **kw):
            if "venv" in argv:
                return _completed(returncode=0)
            return _completed(returncode=1, stdout="network unreachable")

        monkeypatch.setattr(installer, "_run", _fake_run)
        with pytest.raises(installer.InstallerError, match="pip install"):
            installer.ensure_installed()

    def test_missing_executable_after_install_raises(self, monkeypatch):
        # venv + pip "succeed" but the executable never actually appears.
        monkeypatch.setattr(installer, "_run", lambda argv, **kw: _completed())
        with pytest.raises(installer.InstallerError, match="executable not found"):
            installer.ensure_installed()
        assert not installer.STATE_FILE.exists()

    def test_timeout_during_install_raises_installer_error(self, monkeypatch):
        def _fake_run(argv, **kw):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

        monkeypatch.setattr(installer, "_run", _fake_run)
        with pytest.raises(installer.InstallerError, match="timed out"):
            installer.ensure_installed()


class TestUninstall:
    def _make_installed(self) -> None:
        exe = installer.server_executable()
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("", encoding="utf-8")
        installer.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        installer.STATE_FILE.write_text('{"version": "0.1.0"}', encoding="utf-8")
        installer.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        (installer.CONFIG_DIR / "ov.conf").write_text("{}", encoding="utf-8")
        installer.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (installer.DATA_DIR / "index.bin").write_text("x", encoding="utf-8")

    def test_default_keeps_config_and_data(self):
        self._make_installed()

        installer.uninstall()

        assert not installer.VENV_DIR.exists()
        assert not installer.STATE_FILE.exists()
        assert installer.CONFIG_DIR.exists()
        assert installer.DATA_DIR.exists()
        assert installer.is_installed() is False

    def test_remove_data_also_deletes_config_and_data(self):
        self._make_installed()

        installer.uninstall(remove_data=True)

        assert not installer.VENV_DIR.exists()
        assert not installer.STATE_FILE.exists()
        assert not installer.CONFIG_DIR.exists()
        assert not installer.DATA_DIR.exists()

    def test_uninstall_when_nothing_installed_does_not_raise(self):
        installer.uninstall()
        installer.uninstall(remove_data=True)


class TestReadState:
    def test_missing_file_returns_empty_dict(self):
        assert installer.read_state() == {}

    def test_corrupt_file_returns_empty_dict(self):
        installer.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        installer.STATE_FILE.write_text("{not json", encoding="utf-8")
        assert installer.read_state() == {}
