"""Tests for #404: gemini/agy's composer can read READY (correctly — nothing
wrong with `is_at_ready_prompt()`) before its own backend account-eligibility
verification has actually settled, so a delivery landing in that window
pastes onto "Verifying your account... Please try again shortly." and the
task is silently dropped (real user report 2026-08-26, Antigravity CLI
1.1.21 / Gemini 3.7 Flash — pane title showed the delivery header while the
screen still showed the verifying banner).

Covers:
  1. ProviderSpec.post_boot_settle_s / post_boot_settle_s_for() defaults +
     env override.
  2. `_send_when_ready`'s ready-streak gate requires the full settle window
     for a provider that has one (gemini) before its first delivery.
  3. An account_pending marker appearing mid-settle resets the streak
     (waits it out, does not just proceed once the timer alone expires).
  4. A provider with no settle configured (claude, codex, ...) is
     unaffected — unchanged fast-delivery behavior.
  5. A delivery that "settles" but the account-pending gate is back up
     right after (paste swallowed) triggers a bounded auto re-deliver,
     capped at `_POST_BOOT_REDELIVER_MAX`, with a Lead notice once
     exhausted rather than a silent loss.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub import orchestrator as orch_mod
from agent_takkub.lead_inbox import _POST_BOOT_REDELIVER_MAX, _READY_POLL_INTERVAL_MS
from agent_takkub.orchestrator import Orchestrator
from agent_takkub.provider_spec import PROVIDER_REGISTRY, post_boot_settle_s_for


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _live_session() -> MagicMock:
    s = MagicMock()
    s.is_alive = True
    s.write = MagicMock()
    s.is_at_trust_prompt.return_value = False
    s.is_blocked_on_tty_prompt.return_value = None
    s.is_blocked_on_permission_prompt.return_value = None
    s.shows_account_pending_marker.return_value = False
    s.account_pending_reason.return_value = None
    s.auth_failure_reason.return_value = None
    s.shows_boot_phase_marker.return_value = False
    s.seconds_since_output.return_value = 1.0
    return s


def _pane(provider: str, session=None) -> MagicMock:
    p = MagicMock()
    p.session = session
    p.model = MagicMock(provider_name=provider)
    return p


def _written_strings(session: MagicMock) -> list[str]:
    return [
        c.args[0] for c in session.write.call_args_list if c.args and isinstance(c.args[0], str)
    ]


@pytest.fixture
def orch(qapp, monkeypatch) -> Orchestrator:
    o = Orchestrator.__new__(Orchestrator)
    QObject.__init__(o)
    o._panes_by_project = {}
    monkeypatch.setattr(o, "_resolve_project", lambda p=None: p or "P")
    monkeypatch.setattr(
        o, "_project_panes", lambda p=None: o._panes_by_project.get(o._resolve_project(p), {})
    )
    return o


class TestSpecDefaultsAndEnvOverride:
    def test_gemini_has_the_confirmed_settle_window(self) -> None:
        assert PROVIDER_REGISTRY["gemini"].post_boot_settle_s == 8.0

    def test_claude_and_codex_are_unaffected(self) -> None:
        assert PROVIDER_REGISTRY["claude"].post_boot_settle_s == 0.0
        assert PROVIDER_REGISTRY["codex"].post_boot_settle_s == 0.0

    def test_resolver_matches_the_spec_by_default(self) -> None:
        assert post_boot_settle_s_for("gemini") == 8.0
        assert post_boot_settle_s_for("claude") == 0.0

    def test_unknown_provider_is_zero(self) -> None:
        assert post_boot_settle_s_for("not-a-real-provider") == 0.0

    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAKKUB_POST_BOOT_SETTLE_S_GEMINI", "3.5")
        assert post_boot_settle_s_for("gemini") == 3.5

    def test_unparseable_env_override_falls_back_to_spec(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TAKKUB_POST_BOOT_SETTLE_S_GEMINI", "not-a-number")
        assert post_boot_settle_s_for("gemini") == 8.0


class TestSettleGateBeforeFirstDelivery:
    def test_gemini_does_not_deliver_before_the_settle_window(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gemini = _pane("gemini", _live_session())
        gemini.session.is_at_ready_prompt.return_value = True
        lead = _pane("claude", _live_session())
        orch._panes_by_project["P"] = {"lead": lead, "gemini": gemini}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("gemini", "run smoke", max_wait_ms=100_000, project="P")

        assert gemini.session.write.called
        # 8.0s / 150ms = 53.3 -> 54 consecutive ready polls required.
        expected_polls = -(-int(8.0 * 1000) // _READY_POLL_INTERVAL_MS)
        assert expected_polls == 54
        assert gemini.session.is_at_ready_prompt.call_count >= expected_polls

    def test_account_pending_marker_mid_settle_resets_the_wait(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The marker showing for the first few polls must push the
        required wait out by that much, not just delay the START of an
        already-satisfied timer — proves the reset is on the STREAK, not a
        wall-clock deadline computed once up front."""
        gemini = _pane("gemini", _live_session())
        gemini.session.is_at_ready_prompt.return_value = True
        banner_polls = 5
        calls = {"n": 0}

        def _marker(_provider: str) -> bool:
            calls["n"] += 1
            return calls["n"] <= banner_polls

        gemini.session.shows_account_pending_marker.side_effect = _marker
        lead = _pane("claude", _live_session())
        orch._panes_by_project["P"] = {"lead": lead, "gemini": gemini}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("gemini", "run smoke", max_wait_ms=100_000, project="P")

        assert gemini.session.write.called
        # banner_polls resetting the streak each time + 54 clear polls
        # after it finally clears for good.
        assert calls["n"] >= banner_polls + 54


class TestProvidersWithoutSettleAreUnaffected:
    def test_claude_delivers_without_the_gemini_settle_wait(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = _pane("claude", _live_session())
        backend.session.is_at_ready_prompt.return_value = True
        lead = _pane("claude", _live_session())
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("backend", "run smoke", max_wait_ms=1000, project="P")

        assert backend.session.write.called
        # Ordinary flicker-guard + verify-chain polling only — nowhere near
        # gemini's 54 settle-driven polls.
        assert backend.session.is_at_ready_prompt.call_count < 20

    def test_codex_delivers_without_the_gemini_settle_wait(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = _pane("codex", _live_session())
        backend.session.is_at_ready_prompt.return_value = True
        lead = _pane("claude", _live_session())
        orch._panes_by_project["P"] = {"lead": lead, "backend": backend}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("backend", "run smoke", max_wait_ms=1000, project="P")

        assert backend.session.write.called
        assert backend.session.is_at_ready_prompt.call_count < 20


class TestBoundedAutoRedeliverOnPostSettleSwallow:
    """Uses the same `_delayed_enter_verified` interception technique as
    test_delivery_prompt_blocked.py's TestSettledStillOnPromptOverridesAccepted
    — patches it out so `on_settled` can be driven directly instead of
    relying on real verify-grace QTimer chains, and forces
    `post_boot_settle_s_for` to 0 for this class so the poll count stays
    small and the test is about the redeliver BOUND, not the settle wait
    covered above."""

    def test_swallow_retries_then_warns_lead_once_exhausted(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "agent_takkub.provider_spec.post_boot_settle_s_for", lambda _provider: 0.0
        )
        gemini = _pane("gemini", _live_session())

        # required_polls for max_wait_ms=1000/settle=0 is 5: both markers
        # are read once per poll AND once more at each settle check, so a
        # period-6 cycle (5 polls ready+clear, the 6th settled+swallowed)
        # reproduces "reaches ready normally, swallowed right after" on
        # EVERY attempt without needing to time manual return_value flips
        # against the synchronous nested QTimer chain below (on_settled
        # itself kicks off the next attempt's own poll loop before this
        # test ever gets control back).
        def _ready_except_every_sixth():
            calls = {"n": 0}

            def _fn() -> bool:
                calls["n"] += 1
                return calls["n"] % 6 != 0

            return _fn

        def _pending_every_sixth():
            calls = {"n": 0}

            def _fn(_provider: str) -> bool:
                calls["n"] += 1
                return calls["n"] % 6 == 0

            return _fn

        gemini.session.is_at_ready_prompt.side_effect = _ready_except_every_sixth()
        gemini.session.shows_account_pending_marker.side_effect = _pending_every_sixth()
        lead = _pane("claude", _live_session())
        orch._panes_by_project["P"] = {"lead": lead, "gemini": gemini}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with (
            patch("agent_takkub.orchestrator._log_event"),
            patch("agent_takkub.lead_inbox._log_event") as log,
            patch("agent_takkub.orchestrator._delayed_enter_verified") as verified,
        ):
            orch._send_when_ready("gemini", "run smoke", max_wait_ms=1000, project="P")
            # Drive on_settled for the initial delivery + every bounded
            # retry it triggers. Each retry re-enters _send_when_ready
            # synchronously (QTimer stubbed), re-invoking
            # _delayed_enter_verified again — call_args always holds the
            # newest chain's on_settled. Fetched BEFORE calling it each
            # time since the final (exhausted) iteration's on_settled
            # itself triggers ONE further _delayed_enter_verified call —
            # orchestrator.send()'s own peer-message verify chain, used by
            # the Lead notice this test also asserts on below — which is
            # not a task redeliver and must not be miscounted as one.
            for _ in range(_POST_BOOT_REDELIVER_MAX + 1):
                on_settled = verified.call_args.kwargs["on_settled"]
                on_settled()

        task_delivery_calls = [c for c in verified.call_args_list if c.kwargs.get("delivery_id")]
        assert len(task_delivery_calls) == _POST_BOOT_REDELIVER_MAX + 1
        _ps = orch._pane_state[orch_mod._exit_key("P", "gemini")]
        assert _ps.post_boot_redeliver_attempts == _POST_BOOT_REDELIVER_MAX
        assert any(
            c.args and c.args[0] == "task_deliver_post_boot_redeliver_exhausted"
            for c in log.call_args_list
        )
        assert any("[delivery-uncertain]" in m for m in _written_strings(lead.session))

    def test_clears_on_first_try_never_retries(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control case: the ordinary, common path — settles clean, no
        account-pending marker, no retry ever scheduled."""
        monkeypatch.setattr(
            "agent_takkub.provider_spec.post_boot_settle_s_for", lambda _provider: 0.0
        )
        gemini = _pane("gemini", _live_session())
        gemini.session.is_at_ready_prompt.return_value = True
        lead = _pane("claude", _live_session())
        orch._panes_by_project["P"] = {"lead": lead, "gemini": gemini}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with (
            patch("agent_takkub.orchestrator._log_event"),
            patch("agent_takkub.lead_inbox._log_event"),
            patch("agent_takkub.orchestrator._delayed_enter_verified") as verified,
        ):
            orch._send_when_ready("gemini", "run smoke", max_wait_ms=1000, project="P")
            on_settled = verified.call_args.kwargs["on_settled"]
            gemini.session.is_at_ready_prompt.return_value = False
            on_settled()

        assert verified.call_count == 1
        delivery = next(iter(orch._delivery_manager._deliveries.values()))
        assert delivery.state.value == "accepted"
