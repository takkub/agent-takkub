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
from agent_takkub.remote.config import RemoteConfig, TunnelConfig


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
            def __init__(self, parent, *, is_live, current, on_apply, on_logout_all=None):
                captured["is_live"] = is_live
                captured["current"] = current
                captured["on_apply"] = on_apply
                captured["on_logout_all"] = on_logout_all

            def exec(self):
                captured["exec_called"] = True

        monkeypatch.setattr("agent_takkub.remote.settings_dialog.RemoteSettingsDialog", _FakeDialog)
        stub._on_remote_chip_clicked()
        assert captured["is_live"] is True
        assert isinstance(captured["current"], RemoteConfig)
        assert captured["exec_called"] is True
        assert captured["on_apply"] == stub._apply_remote_config
        assert captured["on_logout_all"] == stub._logout_all_remote_sessions


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

    def test_disable_clears_persisted_sessions(self, monkeypatch, tmp_path):
        """#196 requirement 3: disabling remote invalidates every session,
        not just the live in-memory ones (the server is already stopped/gone
        by the time this runs, so there's no live AuthGate left to clear)."""
        import agent_takkub.remote.session_store as session_store

        monkeypatch.setattr(session_store, "_PATH", tmp_path / "sessions.json")
        session_store.save("some-fingerprint", {"tok-hash": 1e15})
        assert session_store.path().exists()

        _isolate_remote_json(monkeypatch, tmp_path)
        stub = _Stub()
        stub._remote = MagicMock()

        stub._apply_remote_config(None, False)

        assert not session_store.path().exists()

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

    def test_ngrok_random_mode_copies_the_captured_url(self, monkeypatch, tmp_path):
        """Mirrors quick-tunnel's scrape-and-copy — ngrok "random" mode has
        no `public_url` known upfront either, so the enable click waits for
        `Tunnel.captured_url` and persists it as `config.public_url`."""
        _isolate_remote_json(monkeypatch, tmp_path)
        stub = _Stub()
        stub._remote = None

        fake_tunnel = MagicMock()
        fake_tunnel.captured_url = "https://abcd1234.ngrok-free.app"
        fake_remote = MagicMock()
        fake_remote._tunnel = fake_tunnel
        fake_remote.config = RemoteConfig(tunnel=TunnelConfig(type="ngrok", url_mode="random"))
        monkeypatch.setattr(
            "agent_takkub.remote.RemoteControl.maybe_start", lambda orch: fake_remote
        )

        config = RemoteConfig(tunnel=TunnelConfig(type="ngrok", url_mode="random"))
        ok, _msg, _pairing_url = stub._apply_remote_config(config, True)

        assert ok is True
        assert fake_remote.config.public_url == "https://abcd1234.ngrok-free.app"
        assert RemoteConfig.load().public_url == "https://abcd1234.ngrok-free.app"


class TestLogoutAllRemoteSessions:
    def test_live_server_delegates_to_authgate(self):
        """When a server is actually running, the live in-memory sessions
        must die immediately (next request, not next restart) — so this
        goes through `AuthGate.logout_all_sessions()`, not just the file."""
        stub = _Stub()
        fake_remote = MagicMock()
        stub._remote = fake_remote

        stub._logout_all_remote_sessions()

        fake_remote._server.auth.logout_all_sessions.assert_called_once()

    def test_not_live_clears_the_persisted_store_directly(self, monkeypatch, tmp_path):
        """No server running (remote off, or between disable and re-enable)
        — nothing to reach through, so clear the on-disk store on its own."""
        import agent_takkub.remote.session_store as session_store

        monkeypatch.setattr(session_store, "_PATH", tmp_path / "sessions.json")
        session_store.save("some-fingerprint", {"tok-hash": 1e15})
        assert session_store.path().exists()

        stub = _Stub()
        stub._remote = None

        stub._logout_all_remote_sessions()

        assert not session_store.path().exists()

    def test_ngrok_fixed_mode_skips_the_scrape_wait(self, monkeypatch, tmp_path):
        """Fixed-domain ngrok already knows its URL (set by `build_config`
        before `_apply_remote_config` ever runs) — the wait/copy block must
        not touch it even though `_tunnel` is present."""
        _isolate_remote_json(monkeypatch, tmp_path)
        stub = _Stub()
        stub._remote = None

        fake_tunnel = MagicMock()
        fake_tunnel.captured_url = "https://takkub.ngrok-free.app"
        fake_remote = MagicMock()
        fake_remote._tunnel = fake_tunnel
        fake_remote.config = RemoteConfig(
            public_url="https://takkub.ngrok-free.app",
            tunnel=TunnelConfig(
                type="ngrok", url_mode="fixed", ngrok_domain="takkub.ngrok-free.app"
            ),
        )
        monkeypatch.setattr(
            "agent_takkub.remote.RemoteControl.maybe_start", lambda orch: fake_remote
        )

        config = RemoteConfig(
            public_url="https://takkub.ngrok-free.app",
            tunnel=TunnelConfig(
                type="ngrok", url_mode="fixed", ngrok_domain="takkub.ngrok-free.app"
            ),
        )
        ok, _msg, _pairing_url = stub._apply_remote_config(config, True)

        assert ok is True
        assert fake_remote.config.public_url == "https://takkub.ngrok-free.app"

    def test_tunnel_failure_rolls_back_and_reports_reason(self, monkeypatch, tmp_path):
        """Bug fix: a requested tunnel (e.g. `credentials_json` copied from
        another machine) that fails to start used to be reported as a
        successful Enable with a pairing URL that would never connect."""
        _isolate_remote_json(monkeypatch, tmp_path)
        stub = _Stub()
        stub._remote = None

        fake_remote = MagicMock()
        fake_remote._tunnel = None
        fake_remote.tunnel_error = "can't read tunnel credentials: [Errno 2] No such file"
        fake_remote.config = RemoteConfig(
            auto_start_tunnel=True,
            tunnel=TunnelConfig(type="cloudflared", credentials_json="c.json"),
        )
        monkeypatch.setattr(
            "agent_takkub.remote.RemoteControl.maybe_start", lambda orch: fake_remote
        )

        config = RemoteConfig(
            tunnel=TunnelConfig(type="cloudflared", credentials_json="c.json"),
        )
        ok, msg, pairing_url = stub._apply_remote_config(config, True)

        assert ok is False
        assert "can't read tunnel credentials" in msg
        assert "Quick tunnel" in msg
        assert pairing_url == ""
        assert stub._remote is None
        fake_remote.stop.assert_called_once()

    def test_quick_mode_tunnel_failure_also_rolls_back(self, monkeypatch, tmp_path):
        """Quick/ngrok modes go through the same `needs_no_credentials_file`
        gate in `RemoteControl._start()` — the rollback check must catch
        them too, not just named-tunnel mode."""
        _isolate_remote_json(monkeypatch, tmp_path)
        stub = _Stub()
        stub._remote = None

        fake_remote = MagicMock()
        fake_remote._tunnel = None
        fake_remote.tunnel_error = "cloudflared exited immediately: exit code 1"
        fake_remote.config = RemoteConfig(auto_start_tunnel=True, tunnel=TunnelConfig(type="quick"))
        monkeypatch.setattr(
            "agent_takkub.remote.RemoteControl.maybe_start", lambda orch: fake_remote
        )

        config = RemoteConfig(tunnel=TunnelConfig(type="quick"))
        ok, msg, _pairing_url = stub._apply_remote_config(config, True)

        assert ok is False
        assert "cloudflared exited immediately" in msg
        assert stub._remote is None

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


class TestRefreshTunnelIndicator:
    """`StatusHeaderMixin._refresh_tunnel_indicator` — the sidebar's top-left
    tunnel dot, separate from the 🌐 Remote status-bar chip above. Reuses
    `RemoteControl.tunnel_error` / `Tunnel.is_alive` rather than tracking a
    duplicate state copy."""

    def test_no_tabs_attr_does_not_raise(self):
        stub = _Stub()
        stub._remote = None
        stub._refresh_tunnel_indicator()  # no `tabs` on the stub — must no-op

    def test_remote_off_paints_off(self):
        stub = _Stub()
        stub.tabs = MagicMock()
        stub._remote = None
        stub._refresh_tunnel_indicator()
        stub.tabs.set_tunnel_status.assert_called_once()
        state, _tooltip = stub.tabs.set_tunnel_status.call_args[0]
        assert state == "off"

    def test_alive_tunnel_paints_running(self):
        stub = _Stub()
        stub.tabs = MagicMock()
        fake_tunnel = MagicMock()
        fake_tunnel.is_alive = True
        stub._remote = MagicMock(_tunnel=fake_tunnel, tunnel_error=None)
        stub._refresh_tunnel_indicator()
        state, _tooltip = stub.tabs.set_tunnel_status.call_args[0]
        assert state == "running"

    def test_tunnel_error_paints_error_with_reason(self):
        stub = _Stub()
        stub.tabs = MagicMock()
        stub._remote = MagicMock(
            _tunnel=None, tunnel_error="cloudflared exited immediately: exit code 1"
        )
        stub._refresh_tunnel_indicator()
        state, tooltip = stub.tabs.set_tunnel_status.call_args[0]
        assert state == "error"
        assert "cloudflared exited immediately" in tooltip

    def test_died_after_clean_start_paints_error(self):
        """No `tunnel_error` was ever set (start() itself succeeded), but the
        process is dead now — the exact silent-death bug this dot exists to
        surface."""
        stub = _Stub()
        stub.tabs = MagicMock()
        dead_tunnel = MagicMock()
        dead_tunnel.is_alive = False
        dead_tunnel.last_output = "connection refused"
        stub._remote = MagicMock(_tunnel=dead_tunnel, tunnel_error=None)
        stub._refresh_tunnel_indicator()
        state, tooltip = stub.tabs.set_tunnel_status.call_args[0]
        assert state == "error"
        assert "stopped unexpectedly" in tooltip
        assert "connection refused" in tooltip

    def test_remote_on_no_tunnel_configured_paints_off(self):
        stub = _Stub()
        stub.tabs = MagicMock()
        stub._remote = MagicMock(_tunnel=None, tunnel_error=None)
        stub._refresh_tunnel_indicator()
        state, _tooltip = stub.tabs.set_tunnel_status.call_args[0]
        assert state == "off"
