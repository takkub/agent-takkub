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

    def test_enabled_starts_a_real_server_and_stop_tears_it_down(self, _isolated):
        # P1: enabled=true now actually starts the HTTP server (§9). bind_port=0
        # lets the OS pick a free ephemeral port so this test never collides
        # with a real cockpit's remote port or another test worker.
        RemoteConfig(enabled=True, bind_port=0, auto_start_tunnel=False).save()
        before = set(threading.enumerate())
        rc = RemoteControl.maybe_start(MagicMock())
        try:
            assert isinstance(rc, RemoteControl)
            assert rc.config.enabled is True
            assert rc._server is not None
            assert rc._server.port != 0
        finally:
            rc.stop()
        assert set(threading.enumerate()) == before


class TestQuickTunnelAutoStart:
    """Addendum: quick-tunnel mode (no domain/credentials file) still
    auto-starts the tunnel subprocess — the auto_start_tunnel gate in
    `_start()` must not require `credentials_json` for this mode."""

    def test_quick_mode_starts_tunnel_without_credentials(self, _isolated, monkeypatch):
        import agent_takkub.remote.tunnel as tunnel_mod

        started = {}

        class _FakeTunnel:
            def __init__(self, tunnel_config, public_url, port):
                started["config"] = tunnel_config

            def start(self):
                started["started"] = True

            def stop(self):
                pass

        monkeypatch.setattr(tunnel_mod, "Tunnel", _FakeTunnel)
        RemoteConfig(
            enabled=True,
            bind_port=0,
            auto_start_tunnel=True,
            tunnel=TunnelConfig(type="quick"),
        ).save()
        rc = RemoteControl.maybe_start(MagicMock())
        try:
            assert started.get("started") is True
            assert started["config"].type == "quick"
        finally:
            rc.stop()

    def test_ngrok_mode_starts_tunnel_without_credentials(self, _isolated, monkeypatch):
        import agent_takkub.remote.tunnel as tunnel_mod

        started = {}

        class _FakeTunnel:
            def __init__(self, tunnel_config, public_url, port):
                started["config"] = tunnel_config

            def start(self):
                started["started"] = True

            def stop(self):
                pass

        monkeypatch.setattr(tunnel_mod, "Tunnel", _FakeTunnel)
        RemoteConfig(
            enabled=True,
            bind_port=0,
            auto_start_tunnel=True,
            tunnel=TunnelConfig(type="ngrok", url_mode="fixed", ngrok_domain="x.ngrok-free.app"),
        ).save()
        rc = RemoteControl.maybe_start(MagicMock())
        try:
            assert started.get("started") is True
            assert started["config"].type == "ngrok"
        finally:
            rc.stop()

    def test_named_mode_without_credentials_does_not_start_tunnel(self, _isolated, monkeypatch):
        import agent_takkub.remote.tunnel as tunnel_mod

        created = {}

        class _FakeTunnel:
            def __init__(self, *a, **kw):
                created["yes"] = True

            def start(self):
                pass

            def stop(self):
                pass

        monkeypatch.setattr(tunnel_mod, "Tunnel", _FakeTunnel)
        RemoteConfig(
            enabled=True,
            bind_port=0,
            auto_start_tunnel=True,
            tunnel=TunnelConfig(type="cloudflared", credentials_json=""),
        ).save()
        rc = RemoteControl.maybe_start(MagicMock())
        try:
            assert "yes" not in created
        finally:
            rc.stop()


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

    def test_save_then_load_round_trip_ngrok_fixed(self, _isolated):
        cfg = RemoteConfig(
            enabled=True,
            tunnel=TunnelConfig(
                type="ngrok", url_mode="fixed", ngrok_domain="takkub.ngrok-free.app"
            ),
        )
        cfg.save()
        loaded = RemoteConfig.load()
        assert loaded == cfg
        assert loaded.tunnel.url_mode == "fixed"
        assert loaded.tunnel.ngrok_domain == "takkub.ngrok-free.app"

    def test_ngrok_fields_default_backward_compatible(self, _isolated):
        """A `tunnel` dict from before this addendum has neither key —
        must load to the same safe defaults as a fresh `TunnelConfig()`."""
        import agent_takkub.remote.config as remote_config

        remote_config.path().parent.mkdir(parents=True, exist_ok=True)
        remote_config.path().write_text(
            json.dumps({"enabled": True, "tunnel": {"type": "quick"}}), encoding="utf-8"
        )
        cfg = RemoteConfig.load()
        assert cfg.tunnel.url_mode == "random"
        assert cfg.tunnel.ngrok_domain == ""
        assert cfg.tunnel.ngrok_bin == ""

    def test_save_then_load_round_trip_ngrok_bin(self, _isolated):
        cfg = RemoteConfig(
            enabled=True,
            tunnel=TunnelConfig(type="ngrok", ngrok_bin="/opt/homebrew/bin/ngrok"),
        )
        cfg.save()
        loaded = RemoteConfig.load()
        assert loaded == cfg
        assert loaded.tunnel.ngrok_bin == "/opt/homebrew/bin/ngrok"

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

    def test_unknown_tunnel_subkey_is_ignored_not_reset(self, _isolated):
        """L1 fix (2026-07-07 audit): an unknown `tunnel` key from a newer
        build used to raise TypeError and silently reset the *entire*
        config to default (remote off) — it must now just be filtered out,
        same as the top-level unknown-key handling."""
        import agent_takkub.remote.config as remote_config

        remote_config.path().parent.mkdir(parents=True, exist_ok=True)
        remote_config.path().write_text(
            json.dumps({"enabled": True, "tunnel": {"bogus_field": 1}}), encoding="utf-8"
        )
        cfg = RemoteConfig.load()
        assert cfg.enabled is True
        assert cfg.tunnel == TunnelConfig()

    def test_unknown_tunnel_subkey_alongside_known_ones_preserves_known(self, _isolated):
        import agent_takkub.remote.config as remote_config

        remote_config.path().parent.mkdir(parents=True, exist_ok=True)
        remote_config.path().write_text(
            json.dumps(
                {
                    "enabled": True,
                    "tunnel": {"type": "bat", "credentials_json": "c.json", "bogus_field": 1},
                }
            ),
            encoding="utf-8",
        )
        cfg = RemoteConfig.load()
        assert cfg.enabled is True
        assert cfg.tunnel == TunnelConfig(type="bat", credentials_json="c.json")

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
        # bind_port=0: OS-assigned ephemeral port, never the real remote
        # port a dev's own running cockpit might already hold.
        RemoteConfig(enabled=True, bind_port=0, auto_start_tunnel=False).save()
        win = self._make_window_stub(monkeypatch)
        try:
            win._boot()
            assert win._remote is not None
            assert win._remote.config.enabled is True
        finally:
            if win._remote is not None:
                win._remote.stop()
