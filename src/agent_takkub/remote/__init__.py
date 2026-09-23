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

from PyQt6.QtCore import QCoreApplication, QThread, QTimer

from .config import RemoteConfig

_log = logging.getLogger(__name__)

__all__ = ["RemoteConfig", "RemoteControl"]

# How often the idle-expire watchdog checks `AuthGate.idle_expired()`.
# Coarse on purpose — this only needs to catch "forgot this was on for
# hours", not fire promptly to the minute.
_IDLE_CHECK_MS = 60_000

# How often `_poll_tunnel_url` looks for the URL a quick/ngrok-random tunnel
# printed. Cheap (one attribute read) and only runs until the URL lands or
# the tunnel process dies, so it can afford to be prompt.
_TUNNEL_URL_POLL_MS = 500


class RemoteControl:
    """Handle returned by `maybe_start` when remote control is enabled.

    Owns everything P1 starts — the HTTP server, the Lead notifier, and
    (optionally) the tunnel subprocess — and tears all of it down again in
    `stop()`, called on idle-expire and on `QCoreApplication.aboutToQuit`.
    """

    def __init__(
        self, config: RemoteConfig, orch, *, on_auto_suspend=None, on_public_url=None
    ) -> None:
        self.config = config
        self._orch = orch
        self._server = None
        self._notifier = None
        self._tunnel = None
        self._idle_timer: QTimer | None = None
        # Quick/ngrok-random only: polls `Tunnel.captured_url` on the Qt
        # thread after start and persists it as `config.public_url` — the
        # one place that happens for a boot-time start (the Settings
        # dialog's Enable flow runs its own 6s poll, but nothing else ever
        # read `captured_url`, so every cockpit restart kept advertising
        # the previous run's dead hostname).
        self._url_timer: QTimer | None = None
        # Called (on the Qt thread, once) with the URL when it lands, so a
        # caller can repaint whatever shows the pairing URL. Optional.
        self._on_public_url = on_public_url
        # #252: the caller's hook for "idle-expire just tore this instance
        # down" — MainWindow wires this to drop its own `self._remote`
        # reference and repaint the 🌐 chip, so the UI doesn't keep claiming
        # remote is live once `_check_idle_expire` has already stopped it.
        # Optional (None) so headless/test callers don't need to supply one.
        self._on_auto_suspend = on_auto_suspend
        # #252: set only by `_check_idle_expire` — distinguishes "the user
        # turned this off" from "the idle watchdog tore it down but
        # `config.enabled` is still True on disk", so the next cockpit boot
        # restarts remote control on its own instead of requiring the user
        # to re-open Settings and click Enable every morning.
        self.auto_suspended = False
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
    def maybe_start(cls, orch, *, on_auto_suspend=None, on_public_url=None) -> RemoteControl | None:
        """Off by default: `enabled=false` returns None before touching any
        thread/socket/file/signal. `enabled=true` starts the real server —
        any failure partway through is cleaned up before returning None
        (B4: never leave a half-open socket/thread behind).

        #197: reaps any tunnel orphaned by a *previous* session first,
        unconditionally — a hard-kill/crash that skipped `stop()` last time
        must be cleaned up on this boot even if this session doesn't want
        remote control on at all. Best-effort, never raises.

        #252: `config.enabled=True` on disk means "the user wants remote
        control on" — that's what gates this, not "was it running the
        moment the cockpit last closed". An idle-auto-suspended session
        leaves `enabled` untouched (see `_check_idle_expire`), so a fresh
        boot the next morning starts remote control back up on its own."""
        self = cls.prepare(orch, on_auto_suspend=on_auto_suspend, on_public_url=on_public_url)
        if self is None:
            return None
        return self if self.finish_start() else None

    @classmethod
    def prepare(cls, orch, *, on_auto_suspend=None, on_public_url=None) -> RemoteControl | None:
        """The BLOCKING half of `maybe_start` — safe to run off the Qt thread.

        #640: `maybe_start` ran entirely inside `MainWindow._boot` on the Qt
        main thread, and this half is slow: an orphan-process sweep, binding
        the HTTP server, a loopback HTTP probe, spawning cloudflared and then
        sleeping to see whether it survived. The boot now runs it on a worker
        and calls `finish_start()` — the Qt half — back on the main thread.

        The one Qt object this half creates is `RemoteHttpServer.bridge`
        (built inside `http_server.start_server`), whose thread affinity is
        whatever thread ran this — `_start_qt` rebuilds it on the Qt thread,
        because a QObject left on a finished worker thread never receives
        the queued `request` signal and every bridged `/api/*` route would
        time out with 504.

        Returns None when remote control is off, or when startup failed (the
        server/tunnel are already torn down in that case)."""
        from . import tunnel as _tunnel_mod

        try:
            _tunnel_mod.reap_orphan_tunnel()
        except Exception:
            _log.exception("remote-control: boot-time orphan-tunnel reap failed")

        config = RemoteConfig.load()
        if not config.enabled:
            return None
        self = cls(config, orch, on_auto_suspend=on_auto_suspend, on_public_url=on_public_url)
        try:
            self._start_blocking()
        except Exception:
            _log.exception("remote-control failed to start — cleaning up")
            self._stop_blocking_parts()
            return None
        return self

    def finish_start(self) -> bool:
        """The Qt half of startup — MUST run on the Qt main thread (it builds
        the Lead notifier's QTimer and the idle QTimer, and connects to
        `aboutToQuit`). False means it failed and everything was stopped."""
        try:
            self._start_qt()
        except Exception:
            _log.exception("remote-control failed to finish starting — cleaning up")
            self.stop()
            return False
        return True

    def _stop_blocking_parts(self) -> None:
        """Tear down only what `_start_blocking` created — no Qt calls, so it
        is safe on the worker thread `prepare` may be running on."""
        if self._tunnel is not None:
            try:
                self._tunnel.stop()
            except Exception:
                _log.exception("remote-control: tunnel stop during failed start")
            self._tunnel = None
        if self._server is not None:
            try:
                self._server.stop()
            except Exception:
                _log.exception("remote-control: server stop during failed start")
            self._server = None

    def _start(self) -> None:
        """Both halves in order, on the calling thread (the Settings dialog's
        Enable flow and tests still start synchronously)."""
        self._start_blocking()
        self._start_qt()

    def _start_blocking(self) -> None:
        # Lazy imports (P0 discipline, X-check C2/U5): none of this network
        # machinery loads unless a config file already says enabled=true.
        from . import diagnostics, http_server, tunnel

        if not self.config.secret_path or not self.config.token:
            self.config.secret_path = self.config.secret_path or secrets.token_urlsafe(16)
            self.config.token = self.config.token or secrets.token_urlsafe(32)
            self.config.save()

        self._server = http_server.start_server(self.config, self._orch)

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

        # A quick/ngrok-random `public_url` on disk is only ever the hostname
        # a previous run was assigned — those providers never hand the same
        # one out twice, so it is dead by now. Forget it (in memory; disk is
        # rewritten once the fresh URL lands, see `_poll_tunnel_url`) so
        # `pairing_url()` answers "" (not ready) instead of a link that 530s.
        if tunnel.scrapes_public_url(self.config.tunnel):
            self.config.public_url = ""

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
        # `Tunnel._verify_started` already does for tunnel startup.
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

    def _start_qt(self) -> None:
        """Everything that creates or connects a Qt object — must run on the
        Qt main thread, after `_start_blocking` has bound the server."""
        from . import notify

        self._bind_bridge_to_this_thread()
        self._notifier = notify.LeadNotifier(self._orch, self._server.broadcaster)

        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.stop)

        self._idle_timer = QTimer()
        self._idle_timer.timeout.connect(self._check_idle_expire)
        self._idle_timer.start(_IDLE_CHECK_MS)

        self._start_tunnel_url_watch()

    def _bind_bridge_to_this_thread(self) -> None:
        """`RemoteHttpServer.__init__` builds its `_Bridge` on whatever
        thread ran `_start_blocking`. At boot (#640) that is a worker that
        has already exited by the time this runs, and a QObject owned by a
        finished thread never gets a queued signal delivered — `moveToThread`
        does not rescue it either (PyQt's slot proxy stays behind on the
        dead thread; verified). The handler threads read `server.bridge`
        per request, so a fresh bridge built here, on the Qt thread, is
        what every later `/api/*` call goes through. The synchronous path
        (Settings → Enable, tests) already built it on this thread and
        keeps it."""
        from . import http_server

        bridge = getattr(self._server, "bridge", None)
        if bridge is not None and bridge.thread() == QThread.currentThread():
            return
        self._server.bridge = http_server._Bridge(self._orch)

    def _start_tunnel_url_watch(self) -> None:
        from . import tunnel

        if self._tunnel is None or not tunnel.scrapes_public_url(self.config.tunnel):
            return
        self._url_timer = QTimer()
        self._url_timer.timeout.connect(self._poll_tunnel_url)
        self._url_timer.start(_TUNNEL_URL_POLL_MS)

    def _stop_tunnel_url_watch(self) -> None:
        if self._url_timer is not None:
            self._url_timer.stop()
            self._url_timer = None

    def _poll_tunnel_url(self) -> None:
        """QTimer slot: record the scraped URL as `config.public_url` (and
        save — the Settings dialog and `reports.build_url` read from disk)
        the moment the tunnel's reader thread captures it. Gives up when the
        tunnel is gone or its process died before printing one; nothing
        more is ever coming then."""
        try:
            tunnel_obj = self._tunnel
            if tunnel_obj is None:
                self._stop_tunnel_url_watch()
                return
            url = getattr(tunnel_obj, "captured_url", None)
            if url:
                self._stop_tunnel_url_watch()
                if url != self.config.public_url:
                    self.config.public_url = url
                    self.config.save()
                    _log.info("remote-control: tunnel URL recorded as public_url")
                if self._on_public_url is not None:
                    try:
                        self._on_public_url(url)
                    except Exception:
                        _log.exception("remote-control on_public_url callback failed")
                return
            if not getattr(tunnel_obj, "is_alive", True):
                _log.warning("remote-control: tunnel exited before printing a public URL")
                self._stop_tunnel_url_watch()
        except Exception:
            # QTimer slot — nothing here may escape into Qt's event loop.
            _log.exception("remote-control: tunnel URL watch failed")
            self._stop_tunnel_url_watch()

    def _check_idle_expire(self) -> None:
        """#252 fix: idle-expire used to write `config.enabled = False` to
        disk — indistinguishable on the next boot from the user having
        clicked Disable. `RemoteControl.maybe_start()` reads exactly that
        flag to decide whether to come up at all, so a phone that went quiet
        overnight (idle_expire_min default 240min) silently killed remote
        control until the user noticed and re-opened Settings to turn it
        back on by hand — no auto-resume, no error, nothing in the UI to
        explain why.

        The security behavior this watchdog exists for is unchanged: the
        HTTP server, notifier and tunnel subprocess are still torn down for
        real via `self.stop()`, the same as before — a paired phone that's
        gone quiet stops being reachable exactly as it did previously. What
        changes is *only* that `config.enabled` stays whatever the user last
        set it to. That's the one bit `maybe_start()` checks at boot, so
        leaving it alone means the next cockpit launch (or the next manual
        Enable) restarts remote control on its own instead of requiring a
        trip to Settings — auto-suspended, not user-disabled."""
        try:
            if self._server is not None and self._server.auth.idle_expired():
                _log.info(
                    "remote-control idle-expired — auto-suspending (config.enabled left as-is)"
                )
                self.auto_suspended = True
                self.stop()
                if self._on_auto_suspend is not None:
                    try:
                        self._on_auto_suspend()
                    except Exception:
                        _log.exception("remote-control on_auto_suspend callback failed")
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
        self._stop_tunnel_url_watch()
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
        self._stop_tunnel_url_watch()
        if self._tunnel is not None:
            self._tunnel.stop()
            self._tunnel = None
