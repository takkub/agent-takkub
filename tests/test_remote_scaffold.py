"""Tests for the remote-control bolt-on scaffold (P0 — off-by-default, no
network yet). See `remote-control-plan/2026-07-07-remote-control.md` §9/§13.
"""

from __future__ import annotations

import importlib
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QThread

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


def _wait_for_threads_to_settle(before: set, *, timeout: float = 5.0) -> set:
    """Poll `threading.enumerate()` until it converges back to `before`.

    `rc.stop()` only joins the server's own accept-loop thread — the
    per-request `ThreadingMixIn` handler thread spawned by `_start()`'s own
    loopback probe (`diagnostics.probe_local`) finishes asynchronously a
    beat later and isn't tracked/joined by anyone. That's normally sub-ms,
    but under a loaded CI runner it can outlast an immediate check. Give it
    a real deadline instead of asserting the instant `stop()` returns — a
    genuine leak (stop() failing to tear a thread down at all) still never
    converges and still fails, just after `timeout` instead of instantly.
    """
    deadline = time.monotonic() + timeout
    after = set(threading.enumerate())
    while after != before and time.monotonic() < deadline:
        time.sleep(0.02)
        after = set(threading.enumerate())
    return after


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
        after = _wait_for_threads_to_settle(before)
        leaked = after - before
        missing = before - after
        assert after == before, f"thread leak after rc.stop(): leaked={leaked} missing={missing}"


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
            # Not URL sanitization — just confirming a human-readable
            # diagnostic note mentions both hostnames; no trust decision
            # is gated on this substring check.
            assert (
                "old.example.com" in rc.hostname_mismatch_note
            )  # codeql[py/incomplete-url-substring-sanitization]
            assert (
                "new.example.com" in rc.hostname_mismatch_note
            )  # codeql[py/incomplete-url-substring-sanitization]
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


class TestIdleAutoSuspend:
    """#252: idle-expire tears the live server down for real but must never
    persist `config.enabled = False` — that's what made remote control stay
    dead every next boot until the user noticed and re-enabled by hand."""

    def test_idle_expire_stops_server_but_leaves_enabled_true_on_disk(self, _isolated):
        RemoteConfig(
            enabled=True,
            bind_port=0,
            auto_start_tunnel=False,
            secret_path="existing-secret",
            token="existing-token",
        ).save()
        rc = RemoteControl.maybe_start(MagicMock())
        assert rc is not None
        assert rc.config.enabled is True
        rc._server.auth.idle_expired = lambda: True

        rc._check_idle_expire()

        assert rc._server is None  # actually torn down — same security behavior
        assert rc.auto_suspended is True
        persisted = RemoteConfig.load()
        assert persisted.enabled is True  # NOT persisted as user-disabled
        assert persisted.secret_path == "existing-secret"
        assert persisted.token == "existing-token"  # #252: no re-pair after idle suspend

    def test_idle_expire_calls_on_auto_suspend_callback(self, _isolated):
        RemoteConfig(enabled=True, bind_port=0, auto_start_tunnel=False).save()
        callback = MagicMock()
        rc = RemoteControl.maybe_start(MagicMock(), on_auto_suspend=callback)
        assert rc is not None
        rc._server.auth.idle_expired = lambda: True

        rc._check_idle_expire()

        callback.assert_called_once()

    def test_a_failing_callback_does_not_escape_the_qtimer_slot(self, _isolated):
        RemoteConfig(enabled=True, bind_port=0, auto_start_tunnel=False).save()

        def _boom():
            raise RuntimeError("chip repaint failed")

        rc = RemoteControl.maybe_start(MagicMock(), on_auto_suspend=_boom)
        assert rc is not None
        rc._server.auth.idle_expired = lambda: True

        rc._check_idle_expire()  # must not raise

        assert rc.auto_suspended is True

    def test_not_yet_idle_leaves_server_running_and_does_not_call_back(self, _isolated):
        RemoteConfig(enabled=True, bind_port=0, auto_start_tunnel=False).save()
        callback = MagicMock()
        rc = RemoteControl.maybe_start(MagicMock(), on_auto_suspend=callback)
        try:
            assert rc is not None
            rc._server.auth.idle_expired = lambda: False

            rc._check_idle_expire()

            assert rc._server is not None
            assert rc.auto_suspended is False
            callback.assert_not_called()
        finally:
            rc.stop()

    def test_reboot_after_idle_suspend_starts_remote_control_again(self, _isolated):
        """The actual field bug: idle-expire overnight used to leave
        `enabled=False` on disk, so the next cockpit boot's `maybe_start()`
        read that and returned None — remote control stayed off until the
        user noticed and re-enabled by hand."""
        RemoteConfig(enabled=True, bind_port=0, auto_start_tunnel=False).save()
        rc = RemoteControl.maybe_start(MagicMock())
        assert rc is not None
        rc._server.auth.idle_expired = lambda: True
        rc._check_idle_expire()

        rc2 = RemoteControl.maybe_start(MagicMock())
        try:
            assert rc2 is not None  # would be None with the old persist-false bug
        finally:
            rc2.stop()


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
# Review 2026-09-23 — boot-time start on a worker thread (#640) must still
# leave a bridge the Qt thread can answer through
# ---------------------------------------------------------------------------


def _pump_until(predicate, timeout: float = 10.0) -> bool:
    app = QCoreApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _get_pumped(url: str, headers: dict) -> tuple[int, bytes]:
    """GET *url* on a background thread while this (Qt) thread pumps the
    event loop — the only way a bridged route can ever answer."""
    result: dict = {}

    def _do() -> None:
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=15
            ) as resp:
                result["value"] = (resp.status, resp.read())
        except urllib.error.HTTPError as exc:
            result["value"] = (exc.code, exc.read())
        except Exception as exc:  # pragma: no cover - surfaced by the assert below
            result["value"] = (0, repr(exc).encode())

    t = threading.Thread(target=_do)
    t.start()
    assert _pump_until(lambda: not t.is_alive())
    t.join(timeout=1)
    return result["value"]


class TestBridgeBuiltOffTheQtThread:
    """remote/__init__.py:172 — `prepare()` runs on a plain worker thread at
    boot, and `http_server.start_server` builds the `_Bridge` QObject there.
    Once that worker exits, Qt has no event loop to deliver the queued
    `request` signal to, so `_Bridge._handle` never ran and every bridged
    `/api/*` route answered 504 "orchestrator did not respond" until the
    user clicked Disable → Enable (whose synchronous path builds the bridge
    on the Qt thread). `finish_start()` must leave a bridge this thread owns."""

    def _prepare_on_a_worker(self):
        box: dict = {}

        def _work() -> None:
            box["rc"] = RemoteControl.prepare(MagicMock())

        worker = threading.Thread(target=_work, name="review-remote-prepare")
        worker.start()
        worker.join(timeout=30)
        assert not worker.is_alive()
        return box["rc"]

    def _patch_projects(self, monkeypatch):
        import agent_takkub.remote.api as api_mod

        monkeypatch.setattr(
            api_mod,
            "projects",
            lambda project, mode: {"projects": [], "mode": mode, "open_tabs": []},
        )

    def test_bridged_route_answers_after_prepare_ran_on_a_worker(self, _isolated, monkeypatch):
        self._patch_projects(monkeypatch)
        RemoteConfig(
            enabled=True,
            bind_port=0,
            auto_start_tunnel=False,
            mode="control",
            secret_path="sek",
            token="tok",
        ).save()
        rc = self._prepare_on_a_worker()
        assert rc is not None
        try:
            # The precondition the bug depends on: the server's bridge was
            # built on (and is still owned by) the now-finished worker.
            assert rc._server.bridge.thread() != QThread.currentThread()

            assert rc.finish_start() is True
            assert rc._server.bridge.thread() == QThread.currentThread()

            status, body = _get_pumped(
                f"http://127.0.0.1:{rc._server.port}/sek/api/projects",
                {"Authorization": "Bearer tok"},
            )
            assert status == 200, body
            assert json.loads(body)["projects"] == []
        finally:
            rc.stop()

    def test_synchronous_start_keeps_the_bridge_it_built(self, _isolated, monkeypatch):
        """Settings → Enable (and every test) runs both halves on the Qt
        thread — that bridge is already right and must not be replaced."""
        self._patch_projects(monkeypatch)
        RemoteConfig(
            enabled=True, bind_port=0, auto_start_tunnel=False, secret_path="sek", token="tok"
        ).save()
        rc = RemoteControl.prepare(MagicMock())
        assert rc is not None
        try:
            before = rc._server.bridge
            assert rc.finish_start() is True
            assert rc._server.bridge is before
            status, _ = _get_pumped(
                f"http://127.0.0.1:{rc._server.port}/sek/api/projects",
                {"Authorization": "Bearer tok"},
            )
            assert status == 200
        finally:
            rc.stop()


# ---------------------------------------------------------------------------
# Review 2026-09-23 — quick/ngrok-random URL must be harvested by
# RemoteControl itself (boot path), not only by the Settings dialog's poll
# ---------------------------------------------------------------------------


class _FakeScrapedTunnel:
    """Stands in for `tunnel.Tunnel` in a scraped mode: `captured_url` lands
    later, on the reader thread, exactly like the real one."""

    instances: ClassVar[list] = []

    def __init__(self, tunnel_config, public_url, port):
        self.public_url_given = public_url
        self.captured_url = None
        self.is_alive = True
        self.stopped = False
        _FakeScrapedTunnel.instances.append(self)

    def start(self):
        pass

    def stop(self):
        self.stopped = True


class TestScrapedTunnelUrlWatch:
    """remote/tunnel.py:461 + remote/__init__.py: after a restart the quick
    tunnel's new `*.trycloudflare.com` hostname was never written anywhere —
    the only readers of `captured_url` lived in the Enable flow, so
    `pairing_url()` and `reports.build_url` kept the previous run's dead
    hostname until the user re-enabled by hand."""

    @pytest.fixture(autouse=True)
    def _fake_tunnel(self, monkeypatch):
        import agent_takkub.remote.tunnel as tunnel_mod

        _FakeScrapedTunnel.instances = []
        monkeypatch.setattr(tunnel_mod, "Tunnel", _FakeScrapedTunnel)

    def _quick_config(self, **overrides) -> RemoteConfig:
        cfg = RemoteConfig(
            enabled=True,
            bind_port=0,
            auto_start_tunnel=True,
            tunnel=TunnelConfig(type="quick"),
            public_url="https://stale-name.trycloudflare.com",
            secret_path="sek",
            token="tok",
        )
        for key, value in overrides.items():
            setattr(cfg, key, value)
        cfg.save()
        return cfg

    def test_stale_url_is_forgotten_and_the_fresh_one_is_saved_when_it_lands(self, _isolated):
        self._quick_config()
        landed: list = []
        rc = RemoteControl.maybe_start(MagicMock(), on_public_url=landed.append)
        assert rc is not None
        try:
            fake = _FakeScrapedTunnel.instances[-1]
            # The previous run's hostname never reaches the tunnel, and the
            # handle stops advertising it while the new one is pending.
            assert fake.public_url_given == ""
            assert rc.config.public_url == ""
            assert rc.config.pairing_url() == ""
            assert rc._url_timer is not None and rc._url_timer.isActive()

            fake.captured_url = "https://fresh-name.trycloudflare.com"
            rc._poll_tunnel_url()

            assert rc.config.public_url == "https://fresh-name.trycloudflare.com"
            assert rc.config.pairing_url().startswith("https://fresh-name.trycloudflare.com/sek/")
            # Persisted: the Settings dialog and `reports.build_url` read disk.
            assert RemoteConfig.load().public_url == "https://fresh-name.trycloudflare.com"
            assert landed == ["https://fresh-name.trycloudflare.com"]
            assert rc._url_timer is None
        finally:
            rc.stop()

    def test_watch_keeps_polling_while_the_url_is_pending(self, _isolated):
        self._quick_config()
        rc = RemoteControl.maybe_start(MagicMock())
        try:
            rc._poll_tunnel_url()
            assert rc._url_timer is not None and rc._url_timer.isActive()
            assert rc.config.public_url == ""
        finally:
            rc.stop()
        assert rc._url_timer is None

    def test_watch_stops_when_the_tunnel_dies_before_printing_a_url(self, _isolated):
        self._quick_config()
        landed: list = []
        rc = RemoteControl.maybe_start(MagicMock(), on_public_url=landed.append)
        try:
            _FakeScrapedTunnel.instances[-1].is_alive = False
            rc._poll_tunnel_url()
            assert rc._url_timer is None
            assert rc.config.public_url == ""
            assert landed == []
        finally:
            rc.stop()

    def test_stop_tunnel_only_stops_the_watch(self, _isolated):
        self._quick_config()
        rc = RemoteControl.maybe_start(MagicMock())
        try:
            assert rc._url_timer is not None
            rc.stop_tunnel_only()
            assert rc._url_timer is None
            assert rc._server is not None
        finally:
            rc.stop()

    def test_ngrok_random_is_watched_too(self, _isolated):
        self._quick_config(
            tunnel=TunnelConfig(type="ngrok", url_mode="random"),
            public_url="https://stale1234.ngrok-free.app",
        )
        rc = RemoteControl.maybe_start(MagicMock())
        try:
            assert _FakeScrapedTunnel.instances[-1].public_url_given == ""
            assert rc._url_timer is not None
            _FakeScrapedTunnel.instances[-1].captured_url = "https://fresh1234.ngrok-free.app"
            rc._poll_tunnel_url()
            assert RemoteConfig.load().public_url == "https://fresh1234.ngrok-free.app"
        finally:
            rc.stop()

    def test_fixed_url_modes_keep_public_url_and_start_no_watch(self, _isolated):
        self._quick_config(
            tunnel=TunnelConfig(type="ngrok", url_mode="fixed", ngrok_domain="x.ngrok-free.app"),
            public_url="https://x.ngrok-free.app",
        )
        rc = RemoteControl.maybe_start(MagicMock())
        try:
            assert _FakeScrapedTunnel.instances[-1].public_url_given == "https://x.ngrok-free.app"
            assert rc.config.public_url == "https://x.ngrok-free.app"
            assert rc._url_timer is None
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
        deferred: list = []
        # #640: the blocking half runs on a worker now; capture it and run
        # the two halves by hand so the test controls the ordering.
        win.cli.run_off_thread = lambda work, then: deferred.append((work, then))
        try:
            win._boot()
            assert win._remote is None, "boot must not block on remote startup"
            assert len(deferred) == 1
            work, then = deferred[0]
            then(work())
            assert win._remote is not None
            assert win._remote.config.enabled is True
        finally:
            if win._remote is not None:
                win._remote.stop()

    def test_boot_does_not_adopt_remote_after_window_closed(self, monkeypatch, tmp_path):
        """#640: if the window starts closing while the worker is still
        bringing remote up, the half-started server must be torn down, not
        attached to a dead window (it would keep serving with nothing to
        stop it)."""
        import agent_takkub.remote.config as remote_config

        monkeypatch.setattr(remote_config, "_PATH", tmp_path / "remote.json")
        RemoteConfig(enabled=True, bind_port=0, auto_start_tunnel=False).save()
        win = self._make_window_stub(monkeypatch)
        deferred: list = []
        win.cli.run_off_thread = lambda work, then: deferred.append((work, then))
        win._boot()
        work, then = deferred[0]
        rc = work()
        assert rc is not None and rc._server is not None
        win._closing_down = True
        then(rc)
        assert win._remote is None
        assert rc._server is None, "the half-started HTTP server must be stopped"

    def test_boot_survives_a_failing_remote_prepare(self, monkeypatch):
        win = self._make_window_stub(monkeypatch)
        import agent_takkub.remote as remote_pkg

        def _boom(*_a, **_kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(remote_pkg.RemoteControl, "prepare", classmethod(_boom))
        deferred: list = []
        win.cli.run_off_thread = lambda work, then: deferred.append((work, then))
        win._boot()
        work, then = deferred[0]
        then(work())  # _work swallows the error and hands back None
        assert win._remote is None
