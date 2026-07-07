"""Remote control — delete-to-uninstall bolt-on.

Full design: `remote-control-plan/2026-07-07-remote-control.md` §4 (module
layout) + §9 (phased plan). P1: `maybe_start` actually stands up the HTTP
server, the Lead-event notifier and (optionally) the tunnel subprocess when
`enabled: true` — off (the default) still costs nothing beyond a JSON stat.

Import discipline (X-check C2/U5): this module may only import `.config` at
top level. Everything network-shaped (http.server, the tunnel subprocess,
the api/notify glue) stays behind the lazy import inside `_start()`, so a
dynamic `importlib.import_module("agent_takkub.remote")` on every boot (see
`main_window.py::_boot`) never costs more than this file's own definitions.
"""

from __future__ import annotations

import logging
import secrets

from PyQt6.QtCore import QCoreApplication, QTimer

from .config import RemoteConfig

_log = logging.getLogger(__name__)

__all__ = ["RemoteConfig", "RemoteControl"]

# How often the idle-expire watchdog checks `AuthGate.idle_expired()`.
# Coarse on purpose — this only needs to catch "forgot this was on for
# hours", not fire promptly to the minute.
_IDLE_CHECK_MS = 60_000


class RemoteControl:
    """Handle returned by `maybe_start` when remote control is enabled.

    Owns everything P1 starts — the HTTP server, the Lead notifier, and
    (optionally) the tunnel subprocess — and tears all of it down again in
    `stop()`, called on idle-expire and on `QCoreApplication.aboutToQuit`.
    """

    def __init__(self, config: RemoteConfig, orch) -> None:
        self.config = config
        self._orch = orch
        self._server = None
        self._notifier = None
        self._tunnel = None
        self._idle_timer: QTimer | None = None

    @classmethod
    def maybe_start(cls, orch) -> RemoteControl | None:
        """Off by default: `enabled=false` returns None before touching any
        thread/socket/file/signal. `enabled=true` starts the real server —
        any failure partway through is cleaned up before returning None
        (B4: never leave a half-open socket/thread behind)."""
        config = RemoteConfig.load()
        if not config.enabled:
            return None
        self = cls(config, orch)
        try:
            self._start()
        except Exception:
            _log.exception("remote-control failed to start — cleaning up")
            self.stop()
            return None
        return self

    def _start(self) -> None:
        # Lazy imports (P0 discipline, X-check C2/U5): none of this network
        # machinery loads unless a config file already says enabled=true.
        from . import http_server, notify, tunnel

        if not self.config.secret_path or not self.config.token:
            self.config.secret_path = self.config.secret_path or secrets.token_urlsafe(16)
            self.config.token = self.config.token or secrets.token_urlsafe(32)
            self.config.save()

        self._server = http_server.start_server(self.config, self._orch)
        self._notifier = notify.LeadNotifier(self._orch, self._server.broadcaster)

        if self.config.auto_start_tunnel and self.config.tunnel.credentials_json:
            try:
                self._tunnel = tunnel.Tunnel(
                    self.config.tunnel, self.config.public_url, self._server.port
                )
                self._tunnel.start()
            except tunnel.TunnelError:
                _log.exception("remote tunnel failed to start — server stays loopback-only")
                self._tunnel = None

        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.stop)

        self._idle_timer = QTimer()
        self._idle_timer.timeout.connect(self._check_idle_expire)
        self._idle_timer.start(_IDLE_CHECK_MS)

    def _check_idle_expire(self) -> None:
        if self._server is not None and self._server.auth.idle_expired():
            _log.info("remote-control idle-expired — disabling")
            self.stop()
            self.config.enabled = False
            self.config.save()

    def stop(self) -> None:
        app = QCoreApplication.instance()
        if app is not None:
            try:
                app.aboutToQuit.disconnect(self.stop)
            except (TypeError, RuntimeError):
                pass
        if self._idle_timer is not None:
            self._idle_timer.stop()
            self._idle_timer = None
        if self._tunnel is not None:
            self._tunnel.stop()
            self._tunnel = None
        if self._notifier is not None:
            self._notifier.stop()
            self._notifier = None
        if self._server is not None:
            self._server.stop()
            self._server = None
