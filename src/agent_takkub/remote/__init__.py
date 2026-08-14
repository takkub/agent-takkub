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
        # Set when `tunnel.start()` raises — the reason a requested tunnel
        # didn't come up, so callers (the Settings dialog's Enable flow) can
        # surface *why* instead of a dead pairing URL with no explanation.
        self.tunnel_error: str | None = None
        # #193: non-fatal warnings from `_start()` for the Settings dialog to
        # surface alongside a pairing URL that DID come up but may not
        # actually be reachable/correct — never block Enable on these, they
        # are "here's a heads up", not "here's why it failed".
        self.port_conflict_note: str | None = None
        self.hostname_mismatch_note: str | None = None
        self.local_probe_note: str | None = None

    @classmethod
    def maybe_start(cls, orch) -> RemoteControl | None:
        """Off by default: `enabled=false` returns None before touching any
        thread/socket/file/signal. `enabled=true` starts the real server —
        any failure partway through is cleaned up before returning None
        (B4: never leave a half-open socket/thread behind).

        #197: reaps any tunnel orphaned by a *previous* session first,
        unconditionally — a hard-kill/crash that skipped `stop()` last time
        must be cleaned up on this boot even if this session doesn't want
        remote control on at all. Best-effort, never raises."""
        from . import tunnel as _tunnel_mod

        try:
            _tunnel_mod.reap_orphan_tunnel()
        except Exception:
            _log.exception("remote-control: boot-time orphan-tunnel reap failed")

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
        from . import diagnostics, http_server, notify, tunnel

        if not self.config.secret_path or not self.config.token:
            self.config.secret_path = self.config.secret_path or secrets.token_urlsafe(16)
            self.config.token = self.config.token or secrets.token_urlsafe(32)
            self.config.save()

        self._server = http_server.start_server(self.config, self._orch)
        self._notifier = notify.LeadNotifier(self._orch, self._server.broadcaster)

        # #193 item 1: `start_server` silently scans forward past a taken
        # port — that's the right behavior (this instance still comes up),
        # but silent is exactly what made the bug invisible. Surface it.
        if self.config.bind_port and self._server.port != self.config.bind_port:
            owner = diagnostics.describe_port_owner(self.config.bind_port)
            self.port_conflict_note = (
                f"Port {self.config.bind_port} was already in use"
                + (f" by {owner}" if owner else " by another process")
                + f" — this instance is using port {self._server.port} instead."
            )
            _log.warning("remote-control: %s", self.port_conflict_note)

        # #193 item 3: an orphaned cloudflared from a previous public_url
        # would otherwise look identical to a healthy one until the user
        # tries the link and it 530s. `reap_orphan_tunnel` (called just
        # above, in `maybe_start`) already clears a truly-orphaned process;
        # this catches the case where config.yml itself is just stale.
        from ..config import RUNTIME_DIR

        self.hostname_mismatch_note = diagnostics.check_ingress_mismatch(
            self.config.public_url, RUNTIME_DIR / "tunnel" / "config.yml"
        )
        if self.hostname_mismatch_note:
            _log.warning("remote-control: %s", self.hostname_mismatch_note)

        # #193 item 2 (local half): a loopback GET is cheap (<10ms,
        # same-machine) and safe to run inline here — proves the bind is
        # real, not just that `Popen`/`bind()` didn't raise. The PUBLIC half
        # of the probe (through the tunnel edge) is deliberately NOT run
        # here: it's a real network call with real latency, and this method
        # runs on the Qt main thread — callers that want it (the Settings
        # dialog, once a pairing URL exists) call `diagnostics.probe_public`
        # themselves, off-thread or accepting the same brief block
        # `_verify_named_started` already does for tunnel startup.
        ok, detail = diagnostics.probe_local(self._server.port, self.config.secret_path)
        if not ok:
            self.local_probe_note = f"Local loopback probe failed: {detail}"
            _log.warning("remote-control: %s", self.local_probe_note)

        # Quick-tunnel (cloudflared) and ngrok modes need neither a
        # credentials file nor a known public_url up front — cloudflared's
        # quick tunnel and ngrok's random URL are both assigned on connect
        # — so they can't gate on `credentials_json` like named-tunnel mode
        # does. ngrok's fixed-domain sub-mode also has no credentials file
        # (its auth lives in the ngrok CLI's own config, set once via
        # `ngrok config add-authtoken` at Enable time in the dialog).
        needs_no_credentials_file = self.config.tunnel.type in ("quick", "ngrok")
        if self.config.auto_start_tunnel and (
            needs_no_credentials_file or self.config.tunnel.credentials_json
        ):
            try:
                self._tunnel = tunnel.Tunnel(
                    self.config.tunnel, self.config.public_url, self._server.port
                )
                self._tunnel.start()
            except tunnel.TunnelError as exc:
                _log.exception("remote tunnel failed to start — server stays loopback-only")
                self._tunnel = None
                self.tunnel_error = str(exc)

        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.stop)

        self._idle_timer = QTimer()
        self._idle_timer.timeout.connect(self._check_idle_expire)
        self._idle_timer.start(_IDLE_CHECK_MS)

    def _check_idle_expire(self) -> None:
        try:
            if self._server is not None and self._server.auth.idle_expired():
                _log.info("remote-control idle-expired — disabling")
                self.stop()
                self.config.enabled = False
                self.config.save()
        except Exception:
            # This is a QTimer slot: no filesystem/tunnel failure may escape
            # into Qt's event loop and abort the whole cockpit.
            _log.exception("remote-control idle-expire cleanup failed")

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

    def stop_tunnel_only(self) -> None:
        """#197 item 5: let the user kill just the tunnel subprocess from
        the UI (e.g. it's misbehaving, or they want to go loopback-only)
        without disabling remote control entirely — the HTTP server and Lead
        notifier stay up. A no-op if no tunnel is running."""
        if self._tunnel is not None:
            self._tunnel.stop()
            self._tunnel = None
