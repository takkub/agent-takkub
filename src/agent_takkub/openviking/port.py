"""port.py — loopback-only port selection for the managed local
openviking-server (`06_SECURITY_PORT.md`): prefer 1933, but never spawn a
second server on top of an already-healthy one, and never bind anything but
127.0.0.1.
"""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass

_log = logging.getLogger(__name__)

DEFAULT_PORT = 1933
_HEALTH_TIMEOUT_S = 2.0


@dataclass(frozen=True, slots=True)
class PortDecision:
    port: int
    # True when *port* is already answering /health as a real OpenViking
    # instance — the caller must use it as-is and must NOT spawn a new
    # process on top of it.
    already_healthy: bool


def is_healthy(url: str, *, timeout: float = _HEALTH_TIMEOUT_S) -> bool:
    """`GET {url}/health` — true only when the body looks like an OpenViking
    health response (`{"status": "ok", ...}` or a `healthy` key), not merely
    "something answered on this port". Never raises."""
    req = urllib.request.Request(f"{url.rstrip('/')}/health", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _log.debug("openviking port: health check for %s failed: %s", url, exc)
        return False
    try:
        data = json.loads(body) if body else {}
    except ValueError:
        return False
    return isinstance(data, dict) and (data.get("status") == "ok" or "healthy" in data)


def _port_is_free(candidate: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", candidate))
        except OSError:
            return False
        return True


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def pick_port(preferred: int = DEFAULT_PORT) -> PortDecision:
    """Try *preferred* first. If something else already owns it, reuse that
    port only when the occupant answers `/health` as a real OpenViking
    instance ("external, not owned" — never spawn a second server on top of
    it); otherwise fall back to an OS-assigned free loopback port.

    Best-effort by nature (TOCTOU: another process can grab the port between
    this check and the caller actually spawning) — a spawn failure on the
    chosen port is the caller's problem to surface, not this function's."""
    if _port_is_free(preferred):
        return PortDecision(port=preferred, already_healthy=False)
    if is_healthy(f"http://127.0.0.1:{preferred}"):
        return PortDecision(port=preferred, already_healthy=True)
    return PortDecision(port=_free_loopback_port(), already_healthy=False)
