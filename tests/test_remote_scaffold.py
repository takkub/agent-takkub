"""Tests for the remote-control bolt-on scaffold (P0 — off-by-default, no
network yet). See `remote-control-plan/2026-07-07-remote-control.md` §9/§13.
"""

from __future__ import annotations

import importlib
import json
import socket
import threading
from unittest.mock import MagicMock, patch

import pytest

import agent_takkub.main_window as mw_mod
from agent_takkub.remote import RemoteControl
from agent_takkub.remote.config import RemoteConfig, TunnelConfig


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    import agent_takkub.remote.config as remote_config

    monkeypatch.setattr(remote_config, "_PATH", tmp_path / "remote.json")


# ---------------------------------------------------------------------------
# maybe_start — off by default, zero resources when off
# ---------------------------------------------------------------------------


class TestMaybeStart:
    def test_disabled_by_default_returns_none(self, _isolated):
        assert RemoteControl.maybe_start(MagicMock()) is None

    def test_off_opens_zero_threads(self, _isolated):
        before = set(threading.enumerate())
        RemoteControl.maybe_start(MagicMock())
        after = set(threading.enumerate())
        assert after == before

    def test_off_opens_zero_sockets(self, _isolated, monkeypatch):
        orig_socket = socket.socket
        created = []

        def _tracking_socket(*a, **kw):
            s = orig_socket(*a, **kw)
            created.append(s)
            return s

        monkeypatch.setattr(socket, "socket", _tracking_socket)
        try:
            RemoteControl.maybe_start(MagicMock())
        finally:
            for s in created:
                s.close()
        assert created == []

    def test_enabled_returns_instance_but_still_no_server(self, _isolated):
        RemoteConfig(enabled=True).save()
        rc = RemoteControl.maybe_start(MagicMock())
        assert isinstance(rc, RemoteControl)
        assert rc.config.enabled is True


# ---------------------------------------------------------------------------
# RemoteConfig — load/save, atomic, missing/corrupt -> default (off)
# ---------------------------------------------------------------------------


class TestRemoteConfig:
    def test_missing_file_returns_default_off(self, _isolated):
        import agent_takkub.remote.config as remote_config

        assert not remote_config.path().exists()
        cfg = RemoteConfig.load()
        assert cfg == RemoteConfig()
        assert cfg.enabled is False
        assert not remote_config.path().exists(), "load() must not create the file"

    def test_save_then_load_round_trip(self, _isolated):
        cfg = RemoteConfig(
            enabled=True,
            mode="control",
            bind_port=9999,
            public_url="https://example.com",
            secret_path="abc123",
            token="tok456",
            tunnel=TunnelConfig(
                type="bat", credentials_json="c.json", cloudflared_bin="/usr/bin/cloudflared"
            ),
            auto_start_tunnel=False,
            idle_expire_min=60,
            lockout_after_fails=3,
            tier2_terminal=True,
        )
        cfg.save()
        assert RemoteConfig.load() == cfg

    def test_corrupt_file_falls_back_to_default(self, _isolated):
        import agent_takkub.remote.config as remote_config

        remote_config.path().parent.mkdir(parents=True, exist_ok=True)
        remote_config.path().write_text("{not json", encoding="utf-8")
        assert RemoteConfig.load() == RemoteConfig()

    def test_non_dict_json_falls_back_to_default(self, _isolated):
        import agent_takkub.remote.config as remote_config

        remote_config.path().parent.mkdir(parents=True, exist_ok=True)
        remote_config.path().write_text("[1, 2, 3]", encoding="utf-8")
        assert RemoteConfig.load() == RemoteConfig()

    def test_corrupt_tunnel_subdict_falls_back_to_default(self, _isolated):
        import agent_takkub.remote.config as remote_config

        remote_config.path().parent.mkdir(parents=True, exist_ok=True)
        remote_config.path().write_text(
            json.dumps({"enabled": True, "tunnel": {"bogus_field": 1}}), encoding="utf-8"
        )
        assert RemoteConfig.load() == RemoteConfig()

    def test_unknown_top_level_keys_are_ignored(self, _isolated):
        import agent_takkub.remote.config as remote_config

        remote_config.path().parent.mkdir(parents=True, exist_ok=True)
        remote_config.path().write_text(
            json.dumps({"enabled": True, "totally_unexpected": "x"}), encoding="utf-8"
        )
        cfg = RemoteConfig.load()
        assert cfg.enabled is True


# ---------------------------------------------------------------------------
# main_window._boot() wiring — no-op when the folder is deleted / import fails
# ---------------------------------------------------------------------------


class TestBootWiring:
    def _make_window_stub(self, monkeypatch):
        with patch.object(mw_mod.MainWindow, "__init__", lambda self: None):
            win = mw_mod.MainWindow.__new__(mw_mod.MainWindow)

        win._status = MagicMock()
        win.cli = MagicMock()
        win.cli.listen.return_value = 54321
        win.orch = MagicMock()
        win.orch.paneRequested = MagicMock()
        win.orch.paneRequested.connect = MagicMock()
        win._lead_first_input_fired = set()

        fake_lead = MagicMock()
        monkeypatch.setattr(mw_mod.MainWindow, "lead_pane", property(lambda self: fake_lead))
        monkeypatch.setattr(mw_mod, "active_project", lambda: ("test-project", None))
        monkeypatch.setattr(mw_mod, "preset_roles_for_active", lambda: [])
        monkeypatch.setattr(mw_mod, "get_open_tabs", lambda: [])
        monkeypatch.setattr(mw_mod.MainWindow, "_refresh_rtk_button", lambda self: None)
        monkeypatch.setattr(
            mw_mod.MainWindow, "_restore_teammates_from_snapshot", lambda self: None
        )
        monkeypatch.setattr(mw_mod.MainWindow, "_open_projects", lambda self: [])
        monkeypatch.setattr(mw_mod.MainWindow, "_persist_open_tabs", lambda self: None)
        return win

    def test_boot_no_op_when_remote_module_missing(self, monkeypatch):
        win = self._make_window_stub(monkeypatch)
        orig_import_module = importlib.import_module

        def _raise_not_found(name, *a, **kw):
            if name == "agent_takkub.remote":
                raise ModuleNotFoundError(name)
            return orig_import_module(name, *a, **kw)

        monkeypatch.setattr(importlib, "import_module", _raise_not_found)
        win._boot()
        assert win._remote is None

    def test_boot_swallows_other_errors_without_leaving_a_handle(self, monkeypatch):
        win = self._make_window_stub(monkeypatch)
        orig_import_module = importlib.import_module

        def _raise_other(name, *a, **kw):
            if name == "agent_takkub.remote":
                raise RuntimeError("boom")
            return orig_import_module(name, *a, **kw)

        monkeypatch.setattr(importlib, "import_module", _raise_other)
        win._boot()  # must not raise
        assert win._remote is None

    def test_boot_starts_remote_handle_when_enabled(self, monkeypatch, tmp_path):
        import agent_takkub.remote.config as remote_config

        monkeypatch.setattr(remote_config, "_PATH", tmp_path / "remote.json")
        RemoteConfig(enabled=True).save()
        win = self._make_window_stub(monkeypatch)
        win._boot()
        assert win._remote is not None
        assert win._remote.config.enabled is True
