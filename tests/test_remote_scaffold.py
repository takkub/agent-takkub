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
    import agent_takkub.remote.tunnel as tunnel_config

    monkeypatch.setattr(remote_config, "_PATH", tmp_path / "remote.json")
    # #197: `maybe_start` now unconditionally calls `reap_orphan_tunnel()`
    # (even when disabled) — must never touch a real dev machine's actual
    # `RUNTIME_DIR/tunnel/tunnel_pid.json` (which could point at a genuinely
    # live cockpit's tunnel) while running this suite.
    monkeypatch.setattr(tunnel_config, "_PID_FILE", tmp_path / "tunnel_pid.json")


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


class TestBootTimeReap:
    """#197: `maybe_start` reaps an orphaned tunnel from a PREVIOUS session
    unconditionally — even when this session's config says disabled."""

    def test_reap_runs_even_when_disabled(self, _isolated, monkeypatch):
        import agent_takkub.remote.tunnel as tunnel_mod

        called = []
        monkeypatch.setattr(tunnel_mod, "reap_orphan_tunnel", lambda: called.append(True))

        assert RemoteControl.maybe_start(MagicMock()) is None
        assert called == [True]

    def test_reap_runs_before_the_enabled_server_starts(self, _isolated, monkeypatch):
        import agent_takkub.remote.tunnel as tunnel_mod

        called = []
        monkeypatch.setattr(tunnel_mod, "reap_orphan_tunnel", lambda: called.append(True))
        RemoteConfig(enabled=True, bind_port=0, auto_start_tunnel=False).save()

        rc = RemoteControl.maybe_start(MagicMock())
        try:
            assert called == [True]
        finally:
            rc.stop()

    def test_a_failing_reap_does_not_block_boot(self, _isolated, monkeypatch):
        import agent_takkub.remote.tunnel as tunnel_mod

        def _boom():
            raise RuntimeError("disk unavailable")

        monkeypatch.setattr(tunnel_mod, "reap_orphan_tunnel", _boom)
        RemoteConfig(enabled=True, bind_port=0, auto_start_tunnel=False).save()

        rc = RemoteControl.maybe_start(MagicMock())
        try:
            assert isinstance(rc, RemoteControl)
        finally:
            rc.stop()


class TestStartDiagnosticNotes:
    """#193: `_start()` surfaces non-fatal diagnostics — a port-fallback, a
    stale ingress hostname, a failed loopback probe — instead of a pairing
    URL that comes up looking healthy but silently doesn't work."""

    def test_no_port_conflict_when_bind_port_matches(self, _isolated):
        RemoteConfig(enabled=True, bind_port=0, auto_start_tunnel=False).save()
        rc = RemoteControl.maybe_start(MagicMock())
        try:
            # bind_port=0 means "let the OS pick" — never counted as a
            # conflict against whatever ephemeral port comes back.
            assert rc.port_conflict_note is None
        finally:
            rc.stop()

    def test_port_conflict_is_reported_with_the_holder(self, _isolated, monkeypatch):
        import agent_takkub.remote.diagnostics as diagnostics_mod
        import agent_takkub.remote.http_server as http_server_mod

        class _FakeServer:
            def __init__(self, port):
                self.port = port
                self.broadcaster = MagicMock()

            def stop(self):
                pass

        # Simulate `start_server`'s own scan-forward already having picked a
        # DIFFERENT port than the one requested.
        monkeypatch.setattr(
            http_server_mod, "start_server", lambda config, orch: _FakeServer(12345)
        )
        monkeypatch.setattr(
            diagnostics_mod, "describe_port_owner", lambda port: "pid 999 (other.exe)"
        )
        RemoteConfig(enabled=True, bind_port=9999, auto_start_tunnel=False).save()

        rc = RemoteControl.maybe_start(MagicMock())
        try:
            assert "9999" in rc.port_conflict_note
            assert "pid 999 (other.exe)" in rc.port_conflict_note
            assert "12345" in rc.port_conflict_note
        finally:
            rc.stop()

    def test_hostname_mismatch_is_reported(self, _isolated, monkeypatch, tmp_path):
        import agent_takkub.config as config_mod

        monkeypatch.setattr(config_mod, "RUNTIME_DIR", tmp_path)
        config_yml_dir = tmp_path / "tunnel"
        config_yml_dir.mkdir(parents=True)
        (config_yml_dir / "config.yml").write_text(
            "tunnel: abc\ningress:\n  - hostname: old.example.com\n    service: http://localhost:1\n",
            encoding="utf-8",
        )
        RemoteConfig(
            enabled=True,
            bind_port=0,
            auto_start_tunnel=False,
            public_url="https://new.example.com",
        ).save()

        rc = RemoteControl.maybe_start(MagicMock())
        try:
            assert "old.example.com" in rc.hostname_mismatch_note
            assert "new.example.com" in rc.hostname_mismatch_note
        finally:
            rc.stop()

    def test_local_probe_failure_is_reported(self, _isolated, monkeypatch):
        import agent_takkub.remote.diagnostics as diagnostics_mod

        monkeypatch.setattr(
            diagnostics_mod, "probe_local", lambda port, secret_path, timeout=2.0: (False, "boom")
        )
        RemoteConfig(enabled=True, bind_port=0, auto_start_tunnel=False).save()

        rc = RemoteControl.maybe_start(MagicMock())
        try:
            assert rc.local_probe_note is not None
            assert "boom" in rc.local_probe_note
        finally:
            rc.stop()

    def test_local_probe_success_leaves_note_none(self, _isolated):
        RemoteConfig(enabled=True, bind_port=0, auto_start_tunnel=False).save()
        rc = RemoteControl.maybe_start(MagicMock())
        try:
            assert rc.local_probe_note is None
        finally:
            rc.stop()

    def test_stop_tunnel_only_leaves_server_running(self, _isolated, monkeypatch):
        import agent_takkub.remote.tunnel as tunnel_mod

        class _FakeTunnel:
            def __init__(self, *a, **kw):
                self.stopped = False

            def start(self):
                pass

            def stop(self):
                self.stopped = True

        monkeypatch.setattr(tunnel_mod, "Tunnel", _FakeTunnel)
        RemoteConfig(
            enabled=True,
            bind_port=0,
            auto_start_tunnel=True,
            tunnel=TunnelConfig(type="quick"),
        ).save()

        rc = RemoteControl.maybe_start(MagicMock())
        try:
            tunnel_obj = rc._tunnel
            assert tunnel_obj is not None
            rc.stop_tunnel_only()
            assert tunnel_obj.stopped is True
            assert rc._tunnel is None
            assert rc._server is not None  # server/notifier untouched
        finally:
            rc.stop()


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

    def test_named_mode_tunnel_error_is_recorded_not_just_logged(self, _isolated, monkeypatch):
        """Bug fix: a `TunnelError` from `Tunnel.start()` used to be logged
        and silently discarded — `RemoteControl` now keeps the reason on
        `tunnel_error` so a caller (the Settings dialog's Enable flow) can
        surface it instead of reporting success with a dead tunnel."""
        import agent_takkub.remote.tunnel as tunnel_mod

        class _FakeTunnel:
            def __init__(self, *a, **kw):
                pass

            def start(self):
                raise tunnel_mod.TunnelError("credentials json is missing a valid TunnelID")

            def stop(self):
                pass

        monkeypatch.setattr(tunnel_mod, "Tunnel", _FakeTunnel)
        RemoteConfig(
            enabled=True,
            bind_port=0,
            auto_start_tunnel=True,
            tunnel=TunnelConfig(type="cloudflared", credentials_json="c.json"),
        ).save()
        rc = RemoteControl.maybe_start(MagicMock())
        try:
            assert rc._tunnel is None
            assert rc.tunnel_error == "credentials json is missing a valid TunnelID"
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
