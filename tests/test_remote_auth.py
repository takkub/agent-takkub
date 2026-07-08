"""Tests for `agent_takkub.remote.auth.AuthGate` — secret-path/token checks,
global lockout, single-use SSE tickets, mode gate, idle-expire. See
remote-control-plan/2026-07-07-remote-control.md §6.3/§7.2/§7.5.
"""

from __future__ import annotations

import time

from agent_takkub.remote.auth import AuthGate, hash_password, verify_password
from agent_takkub.remote.config import RemoteConfig


def _gate(**kw) -> AuthGate:
    return AuthGate(RemoteConfig(secret_path="s3cr3t", token="tok123", **kw))


class TestSecretPath:
    def test_correct_segment_passes(self):
        assert _gate().check_secret_path("s3cr3t") is True

    def test_wrong_segment_fails(self):
        assert _gate().check_secret_path("wrong") is False

    def test_empty_segment_fails(self):
        assert _gate().check_secret_path("") is False

    def test_no_configured_secret_never_passes(self):
        gate = AuthGate(RemoteConfig(secret_path="", token="tok"))
        assert gate.check_secret_path("") is False
        assert gate.check_secret_path("anything") is False


class TestBearerTokenAndLockout:
    def test_correct_token_passes(self):
        assert _gate().check_token("tok123") is True

    def test_wrong_token_fails(self):
        assert _gate().check_token("nope") is False

    def test_missing_token_fails(self):
        assert _gate().check_token(None) is False

    def test_no_configured_token_never_passes(self):
        gate = AuthGate(RemoteConfig(secret_path="s", token=""))
        assert gate.check_token("") is False

    def test_lockout_after_threshold_fails(self):
        gate = _gate(lockout_after_fails=3)
        for _ in range(3):
            assert gate.check_token("wrong") is False
        # Locked out now — even the *correct* token is rejected until cooldown.
        assert gate.check_token("tok123") is False
        assert gate.is_locked_out() is True

    def test_lockout_clears_after_backoff_window(self, monkeypatch):
        gate = _gate(lockout_after_fails=1)
        assert gate.check_token("wrong") is False
        assert gate.is_locked_out() is True
        # Fast-forward past the (short, first-offense) backoff window.
        future = time.time() + 3600
        monkeypatch.setattr(time, "time", lambda: future)
        assert gate.is_locked_out() is False
        assert gate.check_token("tok123") is True

    def test_success_resets_fail_count(self):
        gate = _gate(lockout_after_fails=3)
        gate.check_token("wrong")
        gate.check_token("wrong")
        assert gate.check_token("tok123") is True
        # Two more wrong guesses shouldn't lock out — the streak was reset.
        assert gate.check_token("wrong") is False
        assert gate.check_token("wrong") is False
        assert gate.is_locked_out() is False


class TestSSETicket:
    def test_issued_ticket_is_consumable_once(self):
        gate = _gate()
        ticket = gate.issue_ticket("proj-a")
        assert gate.consume_ticket(ticket) == "proj-a"
        assert gate.consume_ticket(ticket) is None, "ticket must be single-use"

    def test_ticket_carries_its_issued_project_namespace(self):
        gate = _gate()
        ticket = gate.issue_ticket("proj-b")
        assert gate.consume_ticket(ticket) == "proj-b"

    def test_unknown_ticket_rejected(self):
        assert _gate().consume_ticket("bogus") is None

    def test_empty_ticket_rejected(self):
        assert _gate().consume_ticket(None) is None
        assert _gate().consume_ticket("") is None

    def test_expired_ticket_rejected(self, monkeypatch):
        gate = _gate()
        ticket = gate.issue_ticket("proj-a")
        future = time.time() + 3600
        monkeypatch.setattr(time, "time", lambda: future)
        assert gate.consume_ticket(ticket) is None


class TestModeGate:
    def test_view_mode_disallows_control(self):
        gate = AuthGate(RemoteConfig(mode="view"))
        assert gate.allows_control() is False

    def test_control_mode_allows_control(self):
        gate = AuthGate(RemoteConfig(mode="control"))
        assert gate.allows_control() is True


class TestIdleExpire:
    def test_fresh_gate_not_expired(self):
        assert _gate(idle_expire_min=240).idle_expired() is False

    def test_expired_after_idle_window(self, monkeypatch):
        gate = _gate(idle_expire_min=1)
        future = time.time() + 3600
        monkeypatch.setattr(time, "time", lambda: future)
        assert gate.idle_expired() is True

    def test_touch_resets_idle_clock(self, monkeypatch):
        base = 1_000_000.0
        monkeypatch.setattr(time, "time", lambda: base)
        gate = _gate(idle_expire_min=1)
        monkeypatch.setattr(time, "time", lambda: base + 120)
        gate.touch()
        monkeypatch.setattr(time, "time", lambda: base + 150)
        assert gate.idle_expired() is False


class TestPasswordHashing:
    """Third auth factor (addendum 2) — plaintext never persisted, only a
    salted PBKDF2 hash (see `RemoteConfig.password_hash`)."""

    def test_correct_password_verifies(self):
        assert verify_password("hunter2", hash_password("hunter2")) is True

    def test_wrong_password_fails(self):
        assert verify_password("wrong", hash_password("hunter2")) is False

    def test_empty_password_never_verifies(self):
        assert verify_password("", hash_password("hunter2")) is False

    def test_empty_hash_never_verifies(self):
        assert verify_password("hunter2", "") is False

    def test_hash_is_salted_differently_each_time(self):
        assert hash_password("hunter2") != hash_password("hunter2")

    def test_malformed_hash_fails_closed(self):
        assert verify_password("hunter2", "not-a-valid-hash") is False
        assert verify_password("hunter2", "zz$zz") is False


class TestPasswordGate:
    """`check_password`/`password_ok`/session issuance (H1 fix) — the
    server-side half of the third auth factor gate. Password success is
    bound to a per-client session credential, not a server-global flag: a
    bearer token alone (e.g. from a leaked pairing link) is never enough
    once *any* client has logged in — each client must present its own
    `X-Session` token."""

    def _pw_gate(self, password: str = "hunter2", **kw) -> AuthGate:
        return AuthGate(
            RemoteConfig(
                secret_path="s3cr3t", token="tok123", password_hash=hash_password(password), **kw
            )
        )

    def test_no_password_configured_is_always_ok(self):
        gate = _gate()  # _gate() leaves password_hash empty
        assert gate.password_ok(None) is True
        assert gate.password_ok("bogus") is True

    def test_not_yet_verified_blocks(self):
        assert self._pw_gate().password_ok(None) is False

    def test_correct_password_then_issued_session_unlocks_gate(self):
        gate = self._pw_gate()
        assert gate.check_password("hunter2") is True
        session = gate.issue_password_session()
        assert gate.password_ok(session) is True

    def test_check_password_alone_does_not_unlock_the_gate(self):
        """H1 fix: verifying the password no longer flips a global flag —
        the caller must separately mint a session via
        `issue_password_session()`."""
        gate = self._pw_gate()
        assert gate.check_password("hunter2") is True
        assert gate.password_ok(None) is False

    def test_wrong_password_stays_blocked(self):
        gate = self._pw_gate()
        assert gate.check_password("nope") is False
        assert gate.password_ok(None) is False

    def test_session_is_per_client_not_global(self):
        """The defining H1 fix behavior: a bogus/guessed token must not
        unlock the gate just because some *other* client already logged
        in — only a request carrying the actual minted session does."""
        gate = self._pw_gate()
        gate.check_password("hunter2")
        session = gate.issue_password_session()
        assert gate.password_ok("not-the-real-session") is False
        assert gate.password_ok(session) is True

    def test_unknown_session_token_rejected(self):
        assert self._pw_gate().check_password_session("bogus") is False

    def test_empty_session_token_rejected(self):
        gate = self._pw_gate()
        assert gate.check_password_session(None) is False
        assert gate.check_password_session("") is False

    def test_session_expires_after_ttl(self, monkeypatch):
        gate = self._pw_gate(idle_expire_min=10)
        gate.check_password("hunter2")
        session = gate.issue_password_session()
        assert gate.check_password_session(session) is True
        future = time.time() + 3600  # well past a 10-minute idle_expire TTL
        monkeypatch.setattr(time, "time", lambda: future)
        assert gate.check_password_session(session) is False

    def test_session_ttl_falls_back_when_idle_expire_disabled(self, monkeypatch):
        gate = self._pw_gate(idle_expire_min=0)
        gate.check_password("hunter2")
        session = gate.issue_password_session()
        # Within the 4h fallback the session is still valid...
        soon = time.time() + 3600
        monkeypatch.setattr(time, "time", lambda: soon)
        assert gate.check_password_session(session) is True
        # ...but not forever.
        much_later = time.time() + 5 * 3600
        monkeypatch.setattr(time, "time", lambda: much_later)
        assert gate.check_password_session(session) is False

    def test_each_verify_mints_a_distinct_session(self):
        gate = self._pw_gate()
        gate.check_password("hunter2")
        s1 = gate.issue_password_session()
        s2 = gate.issue_password_session()
        assert s1 != s2
        assert gate.check_password_session(s1) is True
        assert gate.check_password_session(s2) is True

    def test_password_and_token_lockouts_are_independent(self):
        """H1 fix: password fails no longer share the token's lockout
        counter — a token fail must not contribute to arming the password
        lockout, and vice versa."""
        gate = self._pw_gate(lockout_after_fails=2)
        assert gate.check_password("wrong") is False
        assert gate.check_token("wrong") is False
        # Only one fail against *each* counter — neither threshold (2) is
        # hit yet, so the correct password must still be accepted.
        assert gate.check_password("hunter2") is True
        assert gate.is_locked_out() is False
        assert gate.is_password_locked_out() is False

    def test_valid_token_does_not_defeat_password_lockout(self):
        """H1 regression (2026-07-07 audit): a leaked-link holder who owns a
        *valid* token but not the password used to reset the shared fail
        counter on every successful `check_token` call, so the password
        lockout never armed no matter how many wrong passwords were tried.
        Interleaving a valid token with wrong passwords must still arm the
        password-specific lockout once the threshold is reached."""
        gate = self._pw_gate(lockout_after_fails=5)
        for i in range(5):
            assert gate.check_token("tok123") is True
            assert gate.check_password(f"wrong{i}") is False
        assert gate.is_password_locked_out() is True
        # Locked out now — even the *correct* password is rejected until
        # the backoff window clears.
        assert gate.check_password("hunter2") is False

    def test_zero_disables_idle_expire(self, monkeypatch):
        gate = _gate(idle_expire_min=0)
        monkeypatch.setattr(time, "time", lambda: time.time() + 10**9)
        assert gate.idle_expired() is False
