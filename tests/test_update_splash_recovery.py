"""Regression tests for issue #62: codex 'update available!' splash recovery.

Two parts:
  1. PtySession.is_at_update_splash() — unit tests for the detector itself.
  2. _check_stuck_panes splash path — sends Enter instead of close→respawn,
     waits for SPLASH_DISMISS_COOLDOWN_S grace period, then falls back.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent_takkub.orchestrator import (
    LEAD,
    SPLASH_DISMISS_COOLDOWN_S,
    STUCK_THRESHOLD_S,
    Orchestrator,
    PaneState,
)
from agent_takkub.pty_session import PtySession

# ── is_at_update_splash() unit tests ─────────────────────────────────────────


def _feed_screen(*lines: str) -> PtySession:
    s = PtySession(cols=80, rows=24)
    s._feed_and_log(("\r\n".join(lines)).encode())
    return s


def test_codex_update_splash_detected() -> None:
    s = _feed_screen(
        "OpenAI Codex (v1.2.3)",
        "update available! run npm i -g @openai/codex",
    )
    assert s.is_at_update_splash() is True


def test_codex_splash_bare_update_available_detected() -> None:
    s = _feed_screen("update available!")
    assert s.is_at_update_splash() is True


def test_gemini_passive_footer_not_splash() -> None:
    # Gemini's update footer is passive — must NOT be treated as a codex splash.
    s = _feed_screen(
        "Gemini CLI update available! 0.46.0 -> 0.47.0",
        "Type your message or @path/to/file",
    )
    assert s.is_at_update_splash() is False


def test_gemini_passive_footer_alone_not_splash() -> None:
    s = _feed_screen("Gemini CLI update available! 0.46.0 -> 0.47.0")
    assert s.is_at_update_splash() is False


def test_normal_screen_not_splash() -> None:
    s = _feed_screen("bypass permissions", "shift+tab to cycle")
    assert s.is_at_update_splash() is False


def test_empty_screen_not_splash() -> None:
    s = _feed_screen("")
    assert s.is_at_update_splash() is False


def test_codex_idle_prompt_no_splash() -> None:
    s = _feed_screen("OpenAI Codex (v1.2.3)")
    assert s.is_at_update_splash() is False


# ── _check_stuck_panes splash path ───────────────────────────────────────────


class _FakeSplashPane:
    """AgentPane stand-in that reports an update splash."""

    def __init__(self, at_splash: bool = True) -> None:
        self.state = "working"
        self._last_output_ts = 1.0
        self._session_cwd = "/x"
        sess = MagicMock()
        sess.is_alive = True
        sess.is_blocked_on_tty_prompt.return_value = None
        sess.is_at_update_splash.return_value = at_splash
        self.session = sess


class _FakeSplashOrch:
    """Minimal orchestrator stub for splash-path tests."""

    def __init__(self) -> None:
        self._panes_by_project: dict = {}
        self._pane_state: dict = {}
        self._idle_state: dict = {}
        self._recent_exits: dict = {}
        self.close_calls: list = []
        self.spawn_calls: list = []

    def _ps(self, key: str) -> PaneState:
        if key not in self._pane_state:
            self._pane_state[key] = PaneState()
        return self._pane_state[key]

    def close(self, role, project=None, suppress_pipeline=False, suppress_auto_chain=False, **_kw):
        self.close_calls.append((role, project or ""))
        return True, "ok"

    def spawn(self, role, cwd=None, project=None, **_kw):
        self.spawn_calls.append((role, cwd, project or ""))
        return True, "ok"

    def _send_when_ready(self, role, task, project=None):
        pass

    def _auto_recover_stuck(self, role, project, pane, now):
        Orchestrator._auto_recover_stuck(self, role, project, pane, now)  # type: ignore[arg-type]

    def _maybe_surface_tty_block(self, key, role, project, prompt_line, now):
        Orchestrator._maybe_surface_tty_block(self, key, role, project, prompt_line, now)  # type: ignore[arg-type]

    def _surface_tty_block_notice(self, role, project, prompt_line):
        pass

    def _check_shell_open_dialog(self, project_name, role, pane, key) -> None:
        pass  # no-op stub — #104 tripwire covered in test_stuck_recover.py


@pytest.fixture(autouse=True)
def _patch_qtimer(monkeypatch: pytest.MonkeyPatch) -> list:
    fired: list = []

    class _ShotCapture:
        @staticmethod
        def singleShot(ms, fn):
            fired.append((ms, fn))
            fn()

    monkeypatch.setattr("agent_takkub.orchestrator.QTimer", _ShotCapture)
    return fired


def _check(fake, now: float) -> None:
    Orchestrator._check_stuck_panes(fake, now)  # type: ignore[arg-type]


class TestUpdateSplashRecovery:
    def _make_stuck_splash_pane(self) -> tuple[_FakeSplashOrch, float]:
        fake = _FakeSplashOrch()
        now = 1_000_000.0
        # Pre-populate content_change_ts so pane is past STUCK_THRESHOLD_S
        key = "proj::codex"
        fake._pane_state[key] = PaneState()
        fake._pane_state[key].last_content_change_ts = now - STUCK_THRESHOLD_S - 1
        fake._panes_by_project["proj"] = {"codex": _FakeSplashPane(at_splash=True)}
        return fake, now

    def test_splash_sends_enter_not_close_respawn(self) -> None:
        fake, now = self._make_stuck_splash_pane()
        _check(fake, now)
        # close→respawn must NOT fire
        assert fake.close_calls == []
        assert fake.spawn_calls == []
        # Enter was written to the session
        fake._panes_by_project["proj"]["codex"].session.write.assert_called_once_with(b"\r")

    def test_splash_sets_dismiss_ts(self) -> None:
        fake, now = self._make_stuck_splash_pane()
        _check(fake, now)
        ps = fake._pane_state["proj::codex"]
        assert ps.splash_dismiss_ts == now

    def test_splash_within_cooldown_waits(self) -> None:
        fake, now = self._make_stuck_splash_pane()
        # First tick: dismiss fired
        _check(fake, now)
        write_count = fake._panes_by_project["proj"]["codex"].session.write.call_count

        # Second tick within cooldown — still showing splash
        now2 = now + SPLASH_DISMISS_COOLDOWN_S - 1
        fake._pane_state["proj::codex"].last_content_change_ts = now2 - STUCK_THRESHOLD_S - 1
        _check(fake, now2)

        # No additional write, no close→respawn
        assert fake._panes_by_project["proj"]["codex"].session.write.call_count == write_count
        assert fake.close_calls == []

    def test_splash_cooldown_expired_falls_back_to_recover(self) -> None:
        fake, now = self._make_stuck_splash_pane()
        # First tick: dismiss sent
        _check(fake, now)

        # Advance past cooldown, still showing splash
        now2 = now + SPLASH_DISMISS_COOLDOWN_S + 1
        fake._pane_state["proj::codex"].last_content_change_ts = now2 - STUCK_THRESHOLD_S - 1
        _check(fake, now2)

        # close→respawn must now fire
        assert ("codex", "proj") in fake.close_calls
        assert any(r == "codex" for r, *_ in fake.spawn_calls)

    def test_no_splash_goes_straight_to_recover(self) -> None:
        fake = _FakeSplashOrch()
        now = 1_000_000.0
        key = "proj::backend"
        fake._pane_state[key] = PaneState()
        fake._pane_state[key].last_content_change_ts = now - STUCK_THRESHOLD_S - 1
        fake._panes_by_project["proj"] = {"backend": _FakeSplashPane(at_splash=False)}
        _check(fake, now)
        assert fake.close_calls == [("backend", "proj")]
        assert fake.spawn_calls

    def test_lead_is_exempt_from_splash_recovery(self) -> None:
        fake = _FakeSplashOrch()
        now = 1_000_000.0
        fake._panes_by_project["proj"] = {LEAD.name: _FakeSplashPane(at_splash=True)}
        _check(fake, now)
        assert fake.close_calls == []
        assert fake.spawn_calls == []
        pane = fake._panes_by_project["proj"][LEAD.name]
        pane.session.write.assert_not_called()
