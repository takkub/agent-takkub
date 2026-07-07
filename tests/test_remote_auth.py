"""Tests for `agent_takkub.remote.auth.AuthGate` — secret-path/token checks,
global lockout, single-use SSE tickets, mode gate, idle-expire. See
remote-control-plan/2026-07-07-remote-control.md §6.3/§7.2/§7.5.
"""

from __future__ import annotations

import time

from agent_takkub.remote.auth import AuthGate
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
        ticket = gate.issue_ticket()
        assert gate.consume_ticket(ticket) is True
        assert gate.consume_ticket(ticket) is False, "ticket must be single-use"

    def test_unknown_ticket_rejected(self):
        assert _gate().consume_ticket("bogus") is False

    def test_empty_ticket_rejected(self):
        assert _gate().consume_ticket(None) is False
        assert _gate().consume_ticket("") is False

    def test_expired_ticket_rejected(self, monkeypatch):
        gate = _gate()
        ticket = gate.issue_ticket()
        future = time.time() + 3600
        monkeypatch.setattr(time, "time", lambda: future)
        assert gate.consume_ticket(ticket) is False


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

    def test_zero_disables_idle_expire(self, monkeypatch):
        gate = _gate(idle_expire_min=0)
        monkeypatch.setattr(time, "time", lambda: time.time() + 10**9)
        assert gate.idle_expired() is False
