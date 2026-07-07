"""Tests for the 🌐 Remote status-bar chip: `StatusHeaderMixin._refresh_remote_chip`
and `UserActionsMixin._on_remote_chip_clicked` / `_apply_remote_config`.

A minimal stub mixing in both plain-Python mixins (no `MainWindow`/Qt
construction needed) stands in for the cockpit shell — same spirit as
`test_remote_scaffold.py::TestBootWiring`'s `MainWindow.__new__()` stub, but
lighter since these two mixins have no `__init__` of their own to bypass.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

from PyQt6.QtWidgets import QPushButton

import agent_takkub.status_header as sh_mod
import agent_takkub.user_actions as ua_mod
from agent_takkub.remote.config import RemoteConfig


class _Stub(sh_mod.StatusHeaderMixin, ua_mod.UserActionsMixin):
    def __init__(self) -> None:
        self._status = MagicMock()
        self.orch = MagicMock()
        self._chip_remote = QPushButton()


def _isolate_remote_json(monkeypatch, tmp_path):
    import agent_takkub.remote.config as remote_config

    monkeypatch.setattr(remote_config, "_PATH", tmp_path / "remote.json")


class TestRefreshRemoteChip:
    def test_hides_chip_when_no_chip_attr(self):
        stub = _Stub()
        del stub._chip_remote
        stub._refresh_remote_chip()  # must not raise

    def test_hides_chip_silently_when_remote_module_missing(self, monkeypatch):
        stub = _Stub()
        orig_import_module = importlib.import_module

        def _raise_not_found(name, *a, **kw):
            if name == "agent_takkub.remote":
                raise ModuleNotFoundError(name)
            return orig_import_module(name, *a, **kw)

        monkeypatch.setattr(importlib, "import_module", _raise_not_found)
        stub._refresh_remote_chip()
        assert stub._chip_remote.isHidden() is True

    def test_shows_off_style_when_remote_is_none(self):
        stub = _Stub()
        stub._remote = None
        stub._refresh_remote_chip()
        assert stub._chip_remote.text() == "🌐 Remote"
        assert stub._chip_remote.isHidden() is False

    def test_shows_on_style_when_remote_is_live(self):
        stub = _Stub()
        stub._remote = MagicMock()
        stub._refresh_remote_chip()
        assert stub._chip_remote.text() == "🌐 Remote ●"

    def test_no_remote_attr_yet_defaults_to_off(self):
        """`_boot()` may call this before `self._remote` is assigned in
        some error path — must not raise, must render OFF."""
        stub = _Stub()
        stub._refresh_remote_chip()
        assert stub._chip_remote.text() == "🌐 Remote"


class TestOnRemoteChipClicked:
    def test_missing_remote_package_shows_status_message(self, monkeypatch):
        stub = _Stub()
        orig_import_module = importlib.import_module

        def _raise_not_found(name, *a, **kw):
            if name.startswith("agent_takkub.remote"):
                raise ModuleNotFoundError(name)
            return orig_import_module(name, *a, **kw)

        monkeypatch.setattr(importlib, "import_module", _raise_not_found)
        stub._on_remote_chip_clicked()
        stub._status.showMessage.assert_called_once()

    def test_opens_dialog_with_current_live_state(self, monkeypatch, tmp_path):
        _isolate_remote_json(monkeypatch, tmp_path)
        stub = _Stub()
        stub._remote = MagicMock()  # currently live

        captured = {}

        class _FakeDialog:
            def __init__(self, parent, *, is_live, current, on_apply):
                captured["is_live"] = is_live
                captured["current"] = current
                captured["on_apply"] = on_apply

            def exec(self):
                captured["exec_called"] = True

        monkeypatch.setattr("agent_takkub.remote.settings_dialog.RemoteSettingsDialog", _FakeDialog)
        stub._on_remote_chip_clicked()
        assert captured["is_live"] is True
        assert isinstance(captured["current"], RemoteConfig)
        assert captured["exec_called"] is True
        assert captured["on_apply"] == stub._apply_remote_config


class TestApplyRemoteConfig:
    def test_enable_starts_remote_and_returns_pairing_url(self, monkeypatch, tmp_path):
        _isolate_remote_json(monkeypatch, tmp_path)
        stub = _Stub()
        stub._remote = None

        fake_remote = MagicMock()
        fake_remote.config.pairing_url.return_value = "https://x.example.com/sek/#token=tok"
        monkeypatch.setattr(
            "agent_takkub.remote.RemoteControl.maybe_start", lambda orch: fake_remote
        )

        config = RemoteConfig()
        ok, msg, pairing_url = stub._apply_remote_config(config, True)

        assert ok is True
        assert msg == ""
        assert pairing_url == "https://x.example.com/sek/#token=tok"
        assert stub._remote is fake_remote
        assert config.enabled is True

    def test_enable_failure_reports_error_and_leaves_remote_none(self, monkeypatch, tmp_path):
        _isolate_remote_json(monkeypatch, tmp_path)
        stub = _Stub()
        stub._remote = None
        monkeypatch.setattr("agent_takkub.remote.RemoteControl.maybe_start", lambda orch: None)

        ok, msg, pairing_url = stub._apply_remote_config(RemoteConfig(), True)

        assert ok is False
        assert msg
        assert pairing_url == ""
        assert stub._remote is None

    def test_disable_stops_old_handle_and_sets_none(self, monkeypatch, tmp_path):
        _isolate_remote_json(monkeypatch, tmp_path)
        stub = _Stub()
        old_remote = MagicMock()
        stub._remote = old_remote

        ok, _msg, pairing_url = stub._apply_remote_config(None, False)

        old_remote.stop.assert_called_once()
        assert stub._remote is None
        assert ok is True
        assert pairing_url == ""

    def test_disable_persists_enabled_false(self, monkeypatch, tmp_path):
        _isolate_remote_json(monkeypatch, tmp_path)
        stub = _Stub()
        stub._remote = None
        RemoteConfig(enabled=True).save()

        stub._apply_remote_config(None, False)

        assert RemoteConfig.load().enabled is False

    def test_reenable_while_live_stops_the_old_handle_first(self, monkeypatch, tmp_path):
        _isolate_remote_json(monkeypatch, tmp_path)
        stub = _Stub()
        old_remote = MagicMock()
        stub._remote = old_remote

        new_remote = MagicMock()
        new_remote.config.pairing_url.return_value = ""
        monkeypatch.setattr(
            "agent_takkub.remote.RemoteControl.maybe_start", lambda orch: new_remote
        )

        stub._apply_remote_config(RemoteConfig(), True)

        old_remote.stop.assert_called_once()
        assert stub._remote is new_remote

    def test_missing_remote_package_fails_gracefully(self, monkeypatch):
        stub = _Stub()
        stub._remote = None
        orig_import_module = importlib.import_module

        def _raise_not_found(name, *a, **kw):
            if name == "agent_takkub.remote":
                raise ModuleNotFoundError(name)
            return orig_import_module(name, *a, **kw)

        monkeypatch.setattr(importlib, "import_module", _raise_not_found)
        ok, msg, pairing_url = stub._apply_remote_config(RemoteConfig(), True)

        assert ok is False
        assert "remote/" in msg
        assert pairing_url == ""
