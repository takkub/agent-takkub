"""Targeted tests for the prompt session-cap watchdog."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_takkub.agent_pane_model import AgentPaneModel
from agent_takkub.orchestrator import Orchestrator
from agent_takkub.roles import LEAD, by_name
from agent_takkub.session_cap import (
    DEFAULT_SESSION_CAP_TOKENS,
    SESSION_CAP_ENV,
    resolve_session_cap_threshold,
)


class TestThresholdResolution:
    def test_default(self) -> None:
        assert resolve_session_cap_threshold(environ={}) == DEFAULT_SESSION_CAP_TOKENS

    def test_qsettings_value_overrides_default(self) -> None:
        assert resolve_session_cap_threshold("210,000", environ={}) == 210_000

    def test_env_wins_over_qsettings(self) -> None:
        assert (
            resolve_session_cap_threshold(
                "210000",
                environ={SESSION_CAP_ENV: "175_000"},
            )
            == 175_000
        )

    def test_invalid_env_falls_back_to_valid_setting(self) -> None:
        assert (
            resolve_session_cap_threshold(
                "190000",
                environ={SESSION_CAP_ENV: "disabled"},
            )
            == 190_000
        )


class TestCrossingState:
    def _model(self) -> AgentPaneModel:
        model = AgentPaneModel(by_name("backend"))
        model.configure_provider("claude", supports_token_meter=True)
        return model

    def test_warns_once_until_prompt_drops(self) -> None:
        model = self._model()
        assert model.observe_session_cap(179_999, 180_000) is False
        assert model.observe_session_cap(180_000, 180_000) is True
        assert model.observe_session_cap(220_000, 180_000) is False

    def test_compaction_rearms_next_crossing(self) -> None:
        model = self._model()
        assert model.observe_session_cap(190_000, 180_000) is True
        assert model.observe_session_cap(80_000, 180_000) is False
        assert model.observe_session_cap(181_000, 180_000) is True

    def test_unsupported_provider_never_warns(self) -> None:
        model = self._model()
        model.configure_provider("codex", supports_token_meter=False)
        assert model.observe_session_cap(500_000, 180_000) is False
        assert model.session_cap_warning_active is False


class _SignalCapture:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def emit(self, *args) -> None:
        self.calls.append(args)


class _Pane:
    def __init__(self, role: str, *, prompt: int = 200_000, ready: bool = True) -> None:
        self.role = LEAD if role == "lead" else by_name(role)
        self.session = MagicMock()
        self.session.is_alive = True
        self.session.is_at_ready_prompt.return_value = ready
        self._usage = {"prompt": prompt}
        self.state_calls: list[tuple[str, str | None]] = []

    def current_usage(self):
        return self._usage

    def set_state(self, state: str, note: str | None = None) -> None:
        self.state_calls.append((state, note))


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.sessionCapNotice = _SignalCapture()
        self.queued: list[tuple] = []

    def _project_ns_for_pane(self, _pane) -> str:
        return "project-a"

    def _queue_session_cap_advice(self, *args) -> None:
        self.queued.append(args)


@pytest.fixture(autouse=True)
def _quiet_event_log(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("agent_takkub.orchestrator._log_event", MagicMock())


class TestRoleRouting:
    def test_lead_gets_ui_notice_only(self) -> None:
        fake = _FakeOrchestrator()
        pane = _Pane("lead")

        Orchestrator._on_session_cap_exceeded(fake, pane, 205_000, 180_000)

        assert fake.sessionCapNotice.calls == [("project-a", "lead", 205_000, 180_000, True)]
        assert fake.queued == []
        pane.session.write.assert_not_called()

    def test_teammate_gets_ui_notice_and_safe_queue(self) -> None:
        fake = _FakeOrchestrator()
        pane = _Pane("backend")

        Orchestrator._on_session_cap_exceeded(fake, pane, 205_000, 180_000)

        assert fake.sessionCapNotice.calls == [("project-a", "backend", 205_000, 180_000, False)]
        assert fake.queued == [(pane, "project-a", 205_000, 180_000)]


class TestSafeTeammateDelivery:
    def test_busy_current_turn_is_never_interrupted(self) -> None:
        pane = _Pane("backend", ready=False)
        fake = SimpleNamespace()

        result = Orchestrator._try_session_cap_advice(
            fake, pane, pane.session, "project-a", 205_000, 180_000
        )

        assert result == "wait"
        pane.session.write.assert_not_called()

    def test_compacted_session_cancels_queued_advice(self) -> None:
        pane = _Pane("backend", prompt=90_000, ready=True)
        fake = SimpleNamespace()

        result = Orchestrator._try_session_cap_advice(
            fake, pane, pane.session, "project-a", 205_000, 180_000
        )

        assert result == "stop"
        pane.session.write.assert_not_called()

    def test_ready_teammate_receives_advice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pane = _Pane("backend", ready=True)
        fake = SimpleNamespace()
        verified = MagicMock()
        monkeypatch.setattr("agent_takkub.orchestrator._delayed_enter_verified", verified)

        result = Orchestrator._try_session_cap_advice(
            fake, pane, pane.session, "project-a", 205_000, 180_000
        )

        assert result == "sent"
        pane.session.write.assert_called_once()
        assert "/compact" in pane.session.write.call_args.args[0]
        verified.assert_called_once()
        assert pane.state_calls == [("working", "session-cap advisory")]
