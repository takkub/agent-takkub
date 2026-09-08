"""Tests for issue #509: Provider feedback / CLI survey prompt auto-skip.

A teammate pane running Gemini (agy) stalled when an interactive survey prompt
("How's the CLI experience so far? [1] Good [2] Fine [3] Bad [0] Skip")
blocked stdin. These tests verify:
1. ProviderSpec configurations and capability matrix for feedback_prompt_skip.
2. PtySession.is_at_feedback_prompt detection on single-line and multiline survey screens.
3. lead_inbox recognition of feedback prompt blocks and auto-skip.
4. orchestrator runtime detection and auto-skip cooldown / max attempts.
5. Watchdog exclusion in stale marker and stuck pane checks.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub.orchestrator import Orchestrator
from agent_takkub.provider_spec import (
    PROVIDER_REGISTRY,
    auto_skip_feedback_for,
    capability_matrix,
    feedback_prompt_markers_for,
    feedback_prompt_skip_key_for,
)
from agent_takkub.pty_session import PtySession
from agent_takkub.roles import LEAD


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _feed_screen(*lines: str) -> PtySession:
    s = PtySession(cols=80, rows=24)
    s._feed_and_log(("\r\n".join(lines)).encode())
    return s


# ---------------------------------------------------------------------------
# 1. ProviderSpec & Multi-Provider Capability Matrix Tests
# ---------------------------------------------------------------------------


def test_gemini_provider_spec_has_feedback_config() -> None:
    spec = PROVIDER_REGISTRY["gemini"]
    assert spec.auto_skip_feedback is True
    assert spec.feedback_prompt_skip_key == "0\r"
    assert any("cli experience so far" in m for m in spec.feedback_prompt_markers)
    assert auto_skip_feedback_for("gemini") is True
    assert feedback_prompt_skip_key_for("gemini") == "0\r"
    assert len(feedback_prompt_markers_for("gemini")) > 0


def test_other_providers_declare_feedback_prompt_gap() -> None:
    for provider in ("claude", "codex", "opencode", "kimi", "cursor"):
        spec = PROVIDER_REGISTRY[provider]
        assert spec.auto_skip_feedback is False
        assert auto_skip_feedback_for(provider) is False
        assert feedback_prompt_markers_for(provider) == ()


def test_capability_matrix_feedback_prompt_skip() -> None:
    gemini_matrix = capability_matrix(PROVIDER_REGISTRY["gemini"])
    assert gemini_matrix["feedback_prompt_skip"] == "supported"

    for provider in ("claude", "codex", "opencode", "kimi", "cursor"):
        matrix = capability_matrix(PROVIDER_REGISTRY[provider])
        assert matrix["feedback_prompt_skip"] == "unsupported"


# ---------------------------------------------------------------------------
# 2. PtySession Detection Tests
# ---------------------------------------------------------------------------


def test_gemini_feedback_prompt_verbatim_survey() -> None:
    """Verbatim survey prompt extracted from agy binary / incident data."""
    s = _feed_screen(
        "How's the CLI experience so far? Help us improve:",
        "[1] Good [2] Fine [3] Bad [0] Skip",
    )
    assert s.is_at_feedback_prompt("gemini") is True
    assert s.is_at_feedback_prompt() is True
    # Feedback prompt must NOT be considered ready
    assert s.is_at_ready_prompt() is False


def test_gemini_feedback_prompt_multiline() -> None:
    s = _feed_screen(
        "Some preceding log line",
        "How's the CLI experience so far?",
        "Help us improve:",
        "[1] Good",
        "[2] Fine",
        "[3] Bad",
        "[0] Skip",
    )
    assert s.is_at_feedback_prompt("gemini") is True


def test_gemini_feedback_prompt_numbered_skip() -> None:
    s = _feed_screen(
        "CLI experience so far:",
        "1. Good",
        "2. Fine",
        "0. Skip",
    )
    assert s.is_at_feedback_prompt("gemini") is True


def test_gemini_feedback_prompt_negative_code() -> None:
    """Code or conversation mentioning 'experience' or 'feedback' is not a survey."""
    s = _feed_screen(
        "def test_user_experience():",
        "    feedback = 'great'",
        "    return feedback",
    )
    assert s.is_at_feedback_prompt("gemini") is False


def test_feedback_prompt_unsupported_provider_returns_false() -> None:
    s = _feed_screen(
        "How's the CLI experience so far?",
        "[0] Skip",
    )
    assert s.is_at_feedback_prompt("claude") is False
    assert s.is_at_feedback_prompt("codex") is False


# ---------------------------------------------------------------------------
# 3. Lead Inbox Delivery Protection Tests
# ---------------------------------------------------------------------------


def test_lead_inbox_recognises_feedback_prompt_reason() -> None:
    from agent_takkub.lead_inbox import _prompt_block_reason

    sess = MagicMock()
    sess.is_at_trust_prompt.return_value = False
    sess.is_blocked_on_tty_prompt.return_value = None
    sess.is_blocked_on_permission_prompt.return_value = None
    sess.is_at_feedback_prompt.return_value = True

    assert _prompt_block_reason(sess) == "feedback"


def test_lead_inbox_warns_lead_on_feedback_prompt(qapp: QCoreApplication) -> None:
    orch = Orchestrator.__new__(Orchestrator)
    QObject.__init__(orch)
    lead_pane = MagicMock()
    lead_pane.session = MagicMock()
    lead_pane.session.is_alive = True
    orch._panes_by_project = {"demo": {LEAD.name: lead_pane}}
    orch._notify_lead = MagicMock()

    orch._warn_lead_delivery_blocked_prompt("frontend", "demo", "feedback")
    assert orch._notify_lead.called
    msg = orch._notify_lead.call_args[0][1]
    assert "CLI survey/feedback prompt" in msg


def test_lead_inbox_send_when_ready_auto_skips_feedback(
    qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch = Orchestrator.__new__(Orchestrator)
    QObject.__init__(orch)
    orch._delivery_in_flight = {}
    orch._notify_lead = MagicMock()

    pane = MagicMock()
    pane.provider = "gemini"
    sess = MagicMock()
    sess.is_alive = True
    sess.is_at_ready_prompt.return_value = False
    sess.is_at_trust_prompt.return_value = False
    sess.is_blocked_on_tty_prompt.return_value = None
    sess.is_blocked_on_permission_prompt.return_value = None
    sess.is_at_feedback_prompt.return_value = True
    sess.write = MagicMock()
    pane.session = sess

    orch._panes_by_project = {"demo": {"frontend": pane}}
    orch._pane = MagicMock(return_value=pane)

    called = False

    def fake_single_shot(_ms, fn):
        nonlocal called
        if not called:
            called = True
            fn()

    monkeypatch.setattr("agent_takkub.lead_inbox.QTimer.singleShot", fake_single_shot)
    orch._send_when_ready("frontend", "test task", 1000, project="demo")
    sess.write.assert_called_with("0\r")


# ---------------------------------------------------------------------------
# 4. Orchestrator Runtime Auto-Skip & Watchdog Tests
# ---------------------------------------------------------------------------


def test_orchestrator_check_feedback_prompts(qapp: QCoreApplication) -> None:
    from agent_takkub.spawn_engine import PaneState

    orch = Orchestrator.__new__(Orchestrator)
    orch._panes_by_project = {}
    orch._pane_state = {}

    pane = MagicMock()
    pane.provider = "gemini"
    pane.state = "working"
    sess = MagicMock()
    sess.is_alive = True
    sess.is_at_feedback_prompt.return_value = True
    sess.write = MagicMock()
    pane.session = sess

    orch._panes_by_project = {"demo": {"frontend": pane}}
    key = "demo::frontend"
    ps = PaneState()
    orch._pane_state[key] = ps

    now = time.time()
    orch._check_feedback_prompts(now)

    sess.write.assert_called_once_with("0\r")
    assert ps.feedback_prompt_dismiss_attempts == 1
    assert ps.feedback_prompt_dismiss_ts == now

    # Cooldown check: second immediate call should not write again
    sess.write.reset_mock()
    orch._check_feedback_prompts(now + 1.0)
    sess.write.assert_not_called()

    # After cooldown (3s), second write allowed
    orch._check_feedback_prompts(now + 3.5)
    sess.write.assert_called_once_with("0\r")
    assert ps.feedback_prompt_dismiss_attempts == 2

    # Third write allowed after cooldown
    sess.write.reset_mock()
    orch._check_feedback_prompts(now + 7.0)
    sess.write.assert_called_once_with("0\r")
    assert ps.feedback_prompt_dismiss_attempts == 3

    # Fourth call rejected because max attempts == 3
    sess.write.reset_mock()
    orch._check_feedback_prompts(now + 11.0)
    sess.write.assert_not_called()


def test_orchestrator_stale_markers_ignores_feedback_prompt(qapp: QCoreApplication) -> None:
    orch = Orchestrator.__new__(Orchestrator)
    orch._panes_by_project = {}
    orch._stale_marker_streak = {"demo::frontend": 2}
    orch._stale_marker_nudged = {}
    orch._stale_marker_last = {}

    pane = MagicMock()
    pane.provider = "gemini"
    sess = MagicMock()
    sess.is_alive = True
    sess.seconds_since_output.return_value = 100.0
    sess.is_at_ready_prompt.return_value = False
    sess.is_blocked_on_tty_prompt.return_value = None
    sess.is_at_trust_prompt.return_value = False
    sess.is_blocked_on_permission_prompt.return_value = None
    sess.is_at_update_splash.return_value = False
    sess.is_at_feedback_prompt.return_value = True
    pane.session = sess

    orch._panes_by_project = {"demo": {"frontend": pane}}
    orch._check_stale_markers(time.time())

    # The streak should be popped (cleared) because feedback prompt is recognised
    assert "demo::frontend" not in orch._stale_marker_streak


def test_spawn_engine_auto_trust_skips_feedback(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_takkub.spawn_engine import PaneState, SpawnEngineMixin

    engine = SpawnEngineMixin()
    engine._pane_state = {}

    pane = MagicMock()
    pane.provider = "gemini"
    sess = MagicMock()
    sess.is_alive = True
    sess.is_at_trust_prompt.return_value = False
    sess.is_at_feedback_prompt.return_value = True
    sess.write = MagicMock()
    pane.session = sess

    engine._project_panes = MagicMock(return_value={"frontend": pane})
    engine._resolve_project = MagicMock(return_value="demo")

    ps = PaneState()
    engine._pane_state["demo::frontend"] = ps

    called = False

    def fake_single_shot(_ms, fn):
        nonlocal called
        if not called:
            called = True
            fn()

    monkeypatch.setattr("agent_takkub.spawn_engine.QTimer.singleShot", fake_single_shot)
    monkeypatch.setattr(
        "agent_takkub.provider_config.effective_provider_for", lambda role, project=None: "gemini"
    )

    engine._auto_trust("frontend", "demo")
    sess.write.assert_called_with("0\r")
    assert ps.feedback_prompt_dismiss_attempts == 1
    assert ps.feedback_prompt_dismiss_ts > 0


def test_orchestrator_check_stuck_panes_skips_feedback(qapp: QCoreApplication) -> None:
    from agent_takkub.spawn_engine import PaneState

    orch = Orchestrator.__new__(Orchestrator)
    orch._panes_by_project = {}
    orch._pane_state = {}
    orch._check_shell_open_dialog = MagicMock()

    pane = MagicMock()
    pane.provider = "gemini"
    pane.state = "working"
    pane._last_output_ts = 1.0
    sess = MagicMock()
    sess.is_alive = True
    sess.display_lines.return_value = ["some static line"]
    sess.is_blocked_on_permission_prompt.return_value = None
    sess.is_blocked_on_tty_prompt.return_value = None
    sess.is_at_update_splash.return_value = False
    sess.is_at_feedback_prompt.return_value = True
    sess.write = MagicMock()
    pane.session = sess

    key = "demo::frontend"
    ps = PaneState()
    ps.last_content_change_ts = 1.0
    orch._pane_state[key] = ps
    orch._panes_by_project = {"demo": {"frontend": pane}}

    now = 1000.0
    orch._check_stuck_panes(now)

    sess.write.assert_called_with("0\r")
    assert ps.feedback_prompt_dismiss_attempts == 1
    assert ps.feedback_prompt_dismiss_ts == now
