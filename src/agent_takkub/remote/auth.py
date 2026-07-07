"""auth.py — bearer-token + secret-path checks, global lockout, and
single-use SSE tickets. See design doc §6.3/§7.2/§7.5 and X-check 3.2/3.3.

Every check here runs on a handler thread (one per HTTP connection, via
`http.server.ThreadingHTTPServer`) — the state below is guarded by a lock
not because simple attribute writes race in Python, but because
check-then-increment fail counting must be atomic or concurrent
brute-force attempts could slip past the lockout threshold.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

from .config import RemoteConfig

_TICKET_TTL_SEC = 30.0
_LOCKOUT_BASE_SEC = 5.0
_LOCKOUT_MAX_SEC = 300.0

# Third auth factor (addendum, user-confirmed): a cockpit-set password, never
# embedded in the pairing URL/QR, so a leaked link alone still can't get in.
# Stored on RemoteConfig.password_hash as "<salt-hex>$<digest-hex>" — the
# plaintext password never touches disk (hash_password runs once, in the
# settings dialog, before RemoteConfig.save()).
_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    """One-way hash for `RemoteConfig.password_hash` — called by the
    settings dialog right before `save()`; the plaintext never reaches
    disk, a log line, or an API response."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time check of `password` against a `hash_password` digest.
    Any malformed `password_hash` (corrupt config, wrong format) fails
    closed rather than raising."""
    if not password or not password_hash:
        return False
    salt_hex, sep, digest_hex = password_hash.partition("$")
    if not sep:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return hmac.compare_digest(actual, expected)


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
        self._tickets: dict[str, tuple[float, str]] = {}
        self.last_request_ts = time.time()
        # Third auth factor: True once a correct password has been POSTed to
        # /api/verify-password this server run. A fresh AuthGate is created
        # per server start (see class docstring), so this — like the fail
        # counter — never needs to survive a restart.
        self._password_verified = False

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
            self._record_result_locked(ok)
            return ok

    # ── password — third auth factor (addendum), never in the pairing URL/QR:
    # a leaked link (secret path + token) still isn't enough to get in. Shares
    # the same global lockout counter as `check_token` per spec (§ADDENDUM 2).
    def check_password(self, password: str) -> bool:
        with self._lock:
            if time.time() < self._locked_until:
                return False
            ok = verify_password(password, self._config.password_hash)
            self._record_result_locked(ok)
            if ok:
                self._password_verified = True
            return ok

    def mark_password_verified(self) -> None:
        """No password configured (`password_hash` empty) = feature off —
        called instead of `check_password` so `password_ok()` passes without
        ever asking the client for one."""
        with self._lock:
            self._password_verified = True

    def password_ok(self) -> bool:
        """Gate for every authenticated route besides verify-password
        itself: true when no password is configured, or once it's been
        verified this server run."""
        if not self._config.password_hash:
            return True
        with self._lock:
            return self._password_verified

    def _record_result_locked(self, ok: bool) -> None:
        """Shared fail-counter/backoff bump for `check_token`/`check_password`
        — call only while holding `self._lock`."""
        if ok:
            self._fail_count = 0
        else:
            self._fail_count += 1
            threshold = max(1, self._config.lockout_after_fails)
            if self._fail_count >= threshold:
                overflow = self._fail_count - threshold
                backoff = min(_LOCKOUT_MAX_SEC, _LOCKOUT_BASE_SEC * (2 ** min(overflow, 6)))
                self._locked_until = time.time() + backoff

    # ── single-use SSE ticket (X-check 3.3): EventSource can't send an
    # Authorization header, so `/api/lead?ticket=...` substitutes a
    # short-lived single-use ticket for the long-lived bearer token, which
    # never has to touch a URL. The ticket also carries the project
    # namespace that was active when it was issued (H-A): the SSE client
    # that consumes it can only ever be scoped to that one project, so a
    # `done`/`lead` event from a different project can never reach it.
    def issue_ticket(self, project_ns: str) -> str:
        with self._lock:
            self._prune_tickets_locked()
            ticket = secrets.token_urlsafe(24)
            self._tickets[ticket] = (time.time() + _TICKET_TTL_SEC, project_ns)
            return ticket

    def consume_ticket(self, ticket: str | None) -> str | None:
        """Returns the project namespace stamped on the ticket, or ``None``
        if the ticket is missing/unknown/expired."""
        if not ticket:
            return None
        with self._lock:
            self._prune_tickets_locked()
            entry = self._tickets.pop(ticket, None)
            if entry is None:
                return None
            expiry, project_ns = entry
            return project_ns if expiry >= time.time() else None

    def _prune_tickets_locked(self) -> None:
        now = time.time()
        for t in [t for t, (exp, _ns) in self._tickets.items() if exp < now]:
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
