"""Tests for the teammate-pane input lock.

Teammate panes are driven entirely by the orchestrator (takkub assign/send),
so the user almost never types into them. An accidental keypress into a
working agent can derail it, so teammate panes default to input-locked: every
USER-originated input (keystroke / image paste / file drop) is dropped before
it reaches the PTY. The Lead pane is the user's command surface and is never
locked. Orchestrator writes go straight to PtySession.write() and are
unaffected by the lock — only manual typing is gated.

These exercise the pure lock logic without a QApplication/QWebEngine by using
``__new__`` instances with the relevant attributes stubbed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agent_takkub.agent_pane import AgentPane
from agent_takkub.roles import LEAD, USER_DRIVEN_ROLES
from agent_takkub.terminal_widget import TerminalWidget


# ─────────────────────────────────────────────────────────────────────
# TerminalWidget — the input chokepoint
# ─────────────────────────────────────────────────────────────────────
class TestTerminalWidgetLock:
    def _make(self, locked: bool) -> TerminalWidget:
        tw = TerminalWidget.__new__(TerminalWidget)
        tw._input_locked = locked
        tw._page_ready = False  # so set_input_locked() skips the JS bridge call
        tw._view = MagicMock()
        tw.inputBytes = MagicMock()
        return tw

    def test_locked_drops_keystroke(self) -> None:
        tw = self._make(locked=True)
        tw._on_input_data("ls\r")
        tw.inputBytes.emit.assert_not_called()

    def test_unlocked_forwards_keystroke(self) -> None:
        tw = self._make(locked=False)
        tw._on_input_data("ls\r")
        tw.inputBytes.emit.assert_called_once_with(b"ls\r")

    def test_locked_drops_pasted_image(self) -> None:
        tw = self._make(locked=True)
        # Must early-return before touching config/disk — no emit, no raise.
        tw._on_image_pasted("ZmFrZQ==", "image/png")
        tw.inputBytes.emit.assert_not_called()

    def test_set_input_locked_toggles_flag(self) -> None:
        tw = self._make(locked=False)
        assert tw.is_input_locked() is False
        tw.set_input_locked(True)
        assert tw.is_input_locked() is True
        tw.set_input_locked(False)
        assert tw.is_input_locked() is False


# ─────────────────────────────────────────────────────────────────────
# AgentPane — default lock per role + toggle
# ─────────────────────────────────────────────────────────────────────
class TestAgentPaneLock:
    def _make(self, *, lockable: bool) -> AgentPane:
        pane = AgentPane.__new__(AgentPane)
        pane._lockable = lockable
        pane._input_locked = lockable  # mirrors __init__ default
        pane._terminal = MagicMock()
        pane._btn_lock = MagicMock() if lockable else None
        return pane

    def test_teammate_defaults_locked(self) -> None:
        pane = self._make(lockable=True)
        assert pane._input_locked is True
        assert pane._btn_lock is not None  # teammate gets the toggle button

    def test_user_driven_pane_never_locked_and_has_no_button(self) -> None:
        pane = self._make(lockable=False)
        assert pane._input_locked is False
        assert pane._btn_lock is None

    def test_toggle_unlocks_then_relocks_teammate(self) -> None:
        pane = self._make(lockable=True)
        pane._toggle_input_lock()
        assert pane._input_locked is False
        pane._terminal.set_input_locked.assert_called_with(False)
        pane._toggle_input_lock()
        assert pane._input_locked is True
        pane._terminal.set_input_locked.assert_called_with(True)

    def test_set_input_locked_is_noop_on_user_driven(self) -> None:
        pane = self._make(lockable=False)
        pane.set_input_locked(True)
        assert pane._input_locked is False  # unchanged
        pane._terminal.set_input_locked.assert_not_called()

    def test_user_driven_roles_membership(self) -> None:
        # The exemption set must cover both the Lead and the ad-hoc Shell pane;
        # orchestrator-driven teammates must NOT be exempt.
        assert LEAD.name in USER_DRIVEN_ROLES
        assert "shell" in USER_DRIVEN_ROLES
        for r in ("frontend", "backend", "qa", "reviewer", "critic", "codex", "gemini"):
            assert r not in USER_DRIVEN_ROLES
