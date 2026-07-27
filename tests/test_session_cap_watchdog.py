"""Targeted tests for the prompt session-cap watchdog."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent_takkub.agent_pane_model import AgentPaneModel
from agent_takkub.orchestrator import Orchestrator
from agent_takkub.roles import LEAD, by_name
from agent_takkub.session_cap import (
    DEFAULT_SESSION_CAP_RATIO,
    SESSION_CAP_ENV,
    SessionCapThreshold,
    resolve_session_cap_threshold,
)


class TestThresholdResolution:
    def test_default_is_ratio_of_context_window(self) -> None:
        spec = resolve_session_cap_threshold(environ={})
        assert spec == SessionCapThreshold(ratio=DEFAULT_SESSION_CAP_RATIO)
        assert spec.tokens_for(200_000) == int(200_000 * DEFAULT_SESSION_CAP_RATIO)
        assert spec.tokens_for(1_000_000) == int(1_000_000 * DEFAULT_SESSION_CAP_RATIO)

    def test_qsettings_ratio_overrides_default(self) -> None:
        spec = resolve_session_cap_threshold("0.5", environ={})
        assert spec == SessionCapThreshold(ratio=0.5)
        assert spec.tokens_for(200_000) == 100_000
        assert spec.tokens_for(1_000_000) == 500_000

    def test_env_wins_over_qsettings(self) -> None:
        spec = resolve_session_cap_threshold(
            "0.5",
            environ={SESSION_CAP_ENV: "0.9"},
        )
        assert spec == SessionCapThreshold(ratio=0.9)

    def test_invalid_env_falls_back_to_valid_setting(self) -> None:
        spec = resolve_session_cap_threshold(
            "0.6",
            environ={SESSION_CAP_ENV: "not-a-number"},
        )
        assert spec == SessionCapThreshold(ratio=0.6)

    def test_zero_disables_watchdog(self) -> None:
        spec = resolve_session_cap_threshold("0", environ={})
        assert spec.tokens_for(200_000) is None
        assert spec.tokens_for(1_000_000) is None

    def test_env_zero_disables_even_with_valid_setting(self) -> None:
        spec = resolve_session_cap_threshold(
            "0.8",
            environ={SESSION_CAP_ENV: "0"},
        )
        assert spec.tokens_for(1_000_000) is None

    def test_legacy_absolute_token_config_still_works(self) -> None:
        spec = resolve_session_cap_threshold("210,000", environ={})
        assert spec == SessionCapThreshold(tokens=210_000)
        # A legacy fixed cap ignores the pane's actual context window.
        assert spec.tokens_for(200_000) == 210_000
        assert spec.tokens_for(1_000_000) == 210_000

    def test_legacy_absolute_token_env_wins(self) -> None:
        spec = resolve_session_cap_threshold(
            "0.5",
            environ={SESSION_CAP_ENV: "175_000"},
        )
        assert spec == SessionCapThreshold(tokens=175_000)


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

    def test_disabled_threshold_never_warns(self) -> None:
        model = self._model()
        assert model.observe_session_cap(999_999, None) is False
        assert model.session_cap_warning_active is False

    def test_ratio_based_threshold_on_1m_window(self) -> None:
        model = self._model()
        spec = SessionCapThreshold(ratio=0.85)
        cap = spec.tokens_for(1_000_000)
        assert model.observe_session_cap(cap - 1, cap) is False
        assert model.observe_session_cap(cap, cap) is True

    def test_ratio_based_threshold_on_200k_window(self) -> None:
        model = self._model()
        spec = SessionCapThreshold(ratio=0.85)
        cap = spec.tokens_for(200_000)
        assert cap == 170_000
        assert model.observe_session_cap(cap - 1, cap) is False
        assert model.observe_session_cap(cap, cap) is True


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

    def _project_ns_for_pane(self, _pane) -> str:
        return "project-a"


@pytest.fixture(autouse=True)
def _quiet_event_log(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("agent_takkub.orchestrator._log_event", MagicMock())


class TestRoleRouting:
    """Both Lead and teammates get a passive UI notice only — no PTY write,
    no advisory queue. Auto-compact inside the CLI handles context pressure;
    the cockpit never pastes into a pane over this."""

    def test_lead_gets_ui_notice_only(self) -> None:
        fake = _FakeOrchestrator()
        pane = _Pane("lead")

        Orchestrator._on_session_cap_exceeded(fake, pane, 205_000, 180_000)

        assert fake.sessionCapNotice.calls == [("project-a", "lead", 205_000, 180_000, True)]
        pane.session.write.assert_not_called()

    def test_teammate_gets_ui_notice_only(self) -> None:
        fake = _FakeOrchestrator()
        pane = _Pane("backend")

        Orchestrator._on_session_cap_exceeded(fake, pane, 205_000, 180_000)

        assert fake.sessionCapNotice.calls == [("project-a", "backend", 205_000, 180_000, False)]
        pane.session.write.assert_not_called()

    def test_no_project_ns_is_a_silent_noop(self) -> None:
        fake = _FakeOrchestrator()
        fake._project_ns_for_pane = lambda _pane: None
        pane = _Pane("backend")

        Orchestrator._on_session_cap_exceeded(fake, pane, 205_000, 180_000)

        assert fake.sessionCapNotice.calls == []
