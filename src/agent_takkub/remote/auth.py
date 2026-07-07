"""auth.py — bearer-token + secret-path checks, global lockout, and
single-use SSE tickets. See design doc §6.3/§7.2/§7.5 and X-check 3.2/3.3.

Every check here runs on a handler thread (one per HTTP connection, via
`http.server.ThreadingHTTPServer`) — the state below is guarded by a lock
not because simple attribute writes race in Python, but because
check-then-increment fail counting must be atomic or concurrent
brute-force attempts could slip past the lockout threshold.
"""

from __future__ import annotations

import secrets
import threading
import time

from .config import RemoteConfig

_TICKET_TTL_SEC = 30.0
_LOCKOUT_BASE_SEC = 5.0
_LOCKOUT_MAX_SEC = 300.0


class AuthGate:
    """Owns the mutable auth state for one running server instance. A fresh
    gate is created per server start — fail counters and tickets don't need
    to survive a restart, since restarting already requires holding the
    token that would be needed to brute-force past a stale lockout anyway.
    """

    def __init__(self, config: RemoteConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._fail_count = 0
        self._locked_until = 0.0
        self._tickets: dict[str, float] = {}
        self.last_request_ts = time.time()

    # ── secret path — second secret ahead of the token (§7.5) ───────────
    def check_secret_path(self, segment: str) -> bool:
        expected = self._config.secret_path
        if not expected or not segment:
            return False
        return secrets.compare_digest(segment.encode(), expected.encode())

    def touch(self) -> None:
        """Record request activity for idle-expire (§6.1). Called once a
        request clears the secret-path check, regardless of what happens
        after — the mobile client polling `/api/pulse` (even on an
        occasionally-wrong token) still counts as "still in use"."""
        self.last_request_ts = time.time()

    # ── bearer token + global lockout (not per-IP: every request arrives
    # from the same tunnel edge / loopback IP, so per-IP counting is a
    # no-op — §7.2) ──────────────────────────────────────────────────────
    def is_locked_out(self) -> bool:
        with self._lock:
            return time.time() < self._locked_until

    def check_token(self, token: str | None) -> bool:
        with self._lock:
            if time.time() < self._locked_until:
                return False
            expected = self._config.token
            ok = (
                bool(token)
                and bool(expected)
                and secrets.compare_digest(token.encode(), expected.encode())
            )
            if ok:
                self._fail_count = 0
            else:
                self._fail_count += 1
                threshold = max(1, self._config.lockout_after_fails)
                if self._fail_count >= threshold:
                    overflow = self._fail_count - threshold
                    backoff = min(_LOCKOUT_MAX_SEC, _LOCKOUT_BASE_SEC * (2 ** min(overflow, 6)))
                    self._locked_until = time.time() + backoff
            return ok

    # ── single-use SSE ticket (X-check 3.3): EventSource can't send an
    # Authorization header, so `/api/lead?ticket=...` substitutes a
    # short-lived single-use ticket for the long-lived bearer token, which
    # never has to touch a URL. ──────────────────────────────────────────
    def issue_ticket(self) -> str:
        with self._lock:
            self._prune_tickets_locked()
            ticket = secrets.token_urlsafe(24)
            self._tickets[ticket] = time.time() + _TICKET_TTL_SEC
            return ticket

    def consume_ticket(self, ticket: str | None) -> bool:
        if not ticket:
            return False
        with self._lock:
            self._prune_tickets_locked()
            expiry = self._tickets.pop(ticket, None)
            return expiry is not None and expiry >= time.time()

    def _prune_tickets_locked(self) -> None:
        now = time.time()
        for t in [t for t, exp in self._tickets.items() if exp < now]:
            self._tickets.pop(t, None)

    # ── mode gate (§6.3): view = read-only, control = unlocks lead/say ──
    def allows_control(self) -> bool:
        return self._config.mode == "control"

    # ── idle-expire (§6.1): no request in idle_expire_min -> auto-disable.
    # RemoteControl polls this from a QTimer and flips config.enabled off.
    def idle_expired(self) -> bool:
        idle_min = self._config.idle_expire_min
        if idle_min <= 0:
            return False
        return (time.time() - self.last_request_ts) > idle_min * 60
