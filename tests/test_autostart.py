"""Start-at-sign-in registration (`autostart`).

The Windows round trip uses its own throwaway value name and a harmless
command, so the suite never registers (or removes) a real cockpit entry.
"""

from __future__ import annotations

import os
import plistlib
import sys

import pytest

from agent_takkub import autostart


def test_launch_args_run_the_gui_module_on_this_interpreter():
    args = autostart.launch_args()
    assert args[1:] == ["-m", "agent_takkub"]
    assert os.path.dirname(args[0]) == os.path.dirname(sys.executable)


def test_windows_prefers_windowless_interpreter(tmp_path, monkeypatch):
    (tmp_path / "python.exe").write_bytes(b"")
    (tmp_path / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    assert autostart.launch_args("win32")[0] == str(tmp_path / "pythonw.exe")
    assert autostart.launch_args("darwin")[0] == str(tmp_path / "python.exe")


def test_launch_command_quotes_a_path_with_spaces(tmp_path, monkeypatch):
    spaced = tmp_path / "my venv"
    spaced.mkdir()
    monkeypatch.setattr(sys, "executable", str(spaced / "python"))
    assert autostart.launch_command("darwin").startswith('"')


def test_dev_and_installed_builds_use_different_names(monkeypatch):
    monkeypatch.setattr(autostart, "_is_dev", lambda: True)
    dev = (autostart.entry_name(), autostart.agent_label())
    monkeypatch.setattr(autostart, "_is_dev", lambda: False)
    installed = (autostart.entry_name(), autostart.agent_label())
    assert dev[0] != installed[0] and dev[1] != installed[1]


def test_mac_plist_round_trip(tmp_path):
    assert autostart._mac_get(tmp_path) is None
    autostart._mac_set(["/venv/bin/python", "-m", "agent_takkub"], tmp_path)
    with autostart.mac_plist_path(tmp_path).open("rb") as fh:
        payload = plistlib.load(fh)
    assert payload["RunAtLoad"] is True
    assert payload["Label"] == autostart.agent_label()
    assert autostart._mac_get(tmp_path) == ["/venv/bin/python", "-m", "agent_takkub"]
    autostart._mac_delete(tmp_path)
    autostart._mac_delete(tmp_path)  # already gone: no error
    assert autostart._mac_get(tmp_path) is None


def test_mac_corrupt_plist_reads_as_off(tmp_path):
    autostart.mac_plist_path(tmp_path).write_bytes(b"not a plist")
    assert autostart._mac_get(tmp_path) is None


def test_unsupported_platform_is_off_and_refuses(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert autostart.is_supported() is False
    assert autostart.is_enabled() is False
    assert autostart.set_enabled(True) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")
def test_windows_registry_round_trip_and_task_manager_switch():
    import winreg

    name = f"TakkubCockpit-pytest-{os.getpid()}"
    try:
        assert autostart._win_get(name) is None
        autostart._win_set(name, "cmd.exe /c rem")
        assert autostart._win_get(name) == "cmd.exe /c rem"
        assert autostart._win_user_disabled(name) is False

        # What Task Manager writes when the user switches the entry off.
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, autostart._WIN_APPROVED_KEY) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_BINARY, b"\x03" + b"\x00" * 11)
        assert autostart._win_user_disabled(name) is True

        # Switching it on from the cockpit wins over that flag.
        autostart._win_set(name, "cmd.exe /c rem")
        assert autostart._win_user_disabled(name) is False
    finally:
        autostart._win_delete(name)
    assert autostart._win_get(name) is None
