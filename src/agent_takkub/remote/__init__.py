"""Remote control — delete-to-uninstall bolt-on (P0 scaffold).

Full design: `remote-control-plan/2026-07-07-remote-control.md` §4 (module
layout) + §9 (phased plan). P0 only wires config + a safe no-op start path —
the HTTP server, SSE, auth, and tunnel pieces described in the design doc are
P1+ and don't exist yet.

Import discipline (X-check C2/U5): this module may only import `.config` at
top level. Anything network-shaped (http.server, ssl, socket, the PWA API
layer) must stay behind a lazy import inside whatever starts it in P1, so
that a dynamic `importlib.import_module("agent_takkub.remote")` on every
boot (see `main_window.py::_boot`) never costs more than a config-file stat.
"""

from __future__ import annotations

import logging

from .config import RemoteConfig

_log = logging.getLogger(__name__)

__all__ = ["RemoteConfig", "RemoteControl"]


class RemoteControl:
    """Handle returned by `maybe_start` when remote control is enabled.

    P0: holds config only — no thread, socket, subprocess, or signal is ever
    opened here. Starting the real server is P1.
    """

    def __init__(self, config: RemoteConfig) -> None:
        self.config = config

    @classmethod
    def maybe_start(cls, orch) -> RemoteControl | None:
        """Off by default: `enabled=false` returns None before touching any
        thread/socket/file/signal. P0 doesn't start a server even when
        enabled — that's P1."""
        config = RemoteConfig.load()
        if not config.enabled:
            return None
        _log.info("remote enabled (P1 not built)")
        return cls(config)
