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
from agent_takkub.terminal_widget import TerminalWidget, split_wheel_reports


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

    def test_locked_forwards_sgr_wheel_reports(self) -> None:
        tw = self._make(locked=True)
        tw.lockedInput = MagicMock()
        # Wheel up (64), wheel down (65)
        tw._on_input_data("\x1b[<64;10;20M")
        tw.inputBytes.emit.assert_called_once_with(b"\x1b[<64;10;20M")
        tw.lockedInput.emit.assert_not_called()

        tw.inputBytes.emit.reset_mock()
        tw._on_input_data("\x1b[<65;15;30M")
        tw.inputBytes.emit.assert_called_once_with(b"\x1b[<65;15;30M")
        tw.lockedInput.emit.assert_not_called()

    def test_locked_drops_sgr_click_and_emits_locked_input(self) -> None:
        tw = self._make(locked=True)
        tw.lockedInput = MagicMock()
        tw._on_input_data("\x1b[<0;10;20M")
        tw.inputBytes.emit.assert_not_called()
        tw.lockedInput.emit.assert_called_once_with("\x1b[<0;10;20M")

    def test_locked_mixed_chunk_splits_wheel_and_locked_input(self) -> None:
        tw = self._make(locked=True)
        tw.lockedInput = MagicMock()
        tw._on_input_data("echo\x1b[<64;10;20M")
        tw.inputBytes.emit.assert_called_once_with(b"\x1b[<64;10;20M")
        tw.lockedInput.emit.assert_called_once_with("echo")

    def test_wheel_input_data_forwards_arrow_escapes(self) -> None:
        tw = self._make(locked=True)
        tw._on_wheel_input_data("\x1b[A")
        tw.inputBytes.emit.assert_called_once_with(b"\x1b[A")

        tw.inputBytes.emit.reset_mock()
        tw._on_wheel_input_data("\x1bOB")
        tw.inputBytes.emit.assert_called_once_with(b"\x1bOB")

    def test_wheel_input_data_rejects_non_escapes(self) -> None:
        tw = self._make(locked=True)
        tw._on_wheel_input_data("plain text")
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


# ─────────────────────────────────────────────────────────────────────
# split_wheel_reports — mouse report filter (#728)
# ─────────────────────────────────────────────────────────────────────
class TestSplitWheelReports:
    def test_plain_text(self) -> None:
        assert split_wheel_reports("hello world") == ("", "hello world")
        assert split_wheel_reports("") == ("", "")
        assert split_wheel_reports("ls -la\r\n") == ("", "ls -la\r\n")

    def test_sgr_wheel_buttons(self) -> None:
        # Wheel up (64), down (65), left (66), right (67)
        assert split_wheel_reports("\x1b[<64;10;20M") == ("\x1b[<64;10;20M", "")
        assert split_wheel_reports("\x1b[<65;10;20M") == ("\x1b[<65;10;20M", "")
        assert split_wheel_reports("\x1b[<66;10;20M") == ("\x1b[<66;10;20M", "")
        assert split_wheel_reports("\x1b[<67;10;20M") == ("\x1b[<67;10;20M", "")

    def test_sgr_wheel_with_modifiers(self) -> None:
        # Shift (+4): 64 + 4 = 68
        assert split_wheel_reports("\x1b[<68;10;20M") == ("\x1b[<68;10;20M", "")
        # Alt (+8): 64 + 8 = 72
        assert split_wheel_reports("\x1b[<72;10;20M") == ("\x1b[<72;10;20M", "")
        # Ctrl (+16): 64 + 16 = 80
        assert split_wheel_reports("\x1b[<80;10;20M") == ("\x1b[<80;10;20M", "")
        # Ctrl+Alt+Shift (+28): 65 + 28 = 93
        assert split_wheel_reports("\x1b[<93;10;20M") == ("\x1b[<93;10;20M", "")

    def test_sgr_non_wheel(self) -> None:
        # Button 1 press (0), button 2 press (1), button 3 press (2)
        assert split_wheel_reports("\x1b[<0;10;20M") == ("", "\x1b[<0;10;20M")
        assert split_wheel_reports("\x1b[<1;10;20M") == ("", "\x1b[<1;10;20M")
        assert split_wheel_reports("\x1b[<2;10;20M") == ("", "\x1b[<2;10;20M")
        # Release (0m)
        assert split_wheel_reports("\x1b[<0;10;20m") == ("", "\x1b[<0;10;20m")
        # Motion with button 0 (32)
        assert split_wheel_reports("\x1b[<32;10;20M") == ("", "\x1b[<32;10;20M")

    def test_x10_wheel(self) -> None:
        # X10 cb character: ord(char) - 32. 64 + 32 = 96 ('`'), 65 + 32 = 97 ('a')
        assert split_wheel_reports("\x1b[M`!!") == ("\x1b[M`!!", "")
        assert split_wheel_reports("\x1b[Ma!!") == ("\x1b[Ma!!", "")
        # Shift modifier (+4): 64 + 4 + 32 = 100 ('d')
        assert split_wheel_reports("\x1b[Md!!") == ("\x1b[Md!!", "")

    def test_x10_non_wheel(self) -> None:
        # Button 1 press: 0 + 32 = 32 (' ')
        assert split_wheel_reports("\x1b[M !!") == ("", "\x1b[M !!")
        # Button 2 press: 1 + 32 = 33 ('!')
        assert split_wheel_reports("\x1b[M!!!") == ("", "\x1b[M!!!")
        # Button release: 3 + 32 = 35 ('#')
        assert split_wheel_reports("\x1b[M#!!") == ("", "\x1b[M#!!")

    def test_multiple_wheel_reports(self) -> None:
        data = "\x1b[<64;10;20M\x1b[<64;10;20M\x1b[<65;10;20M"
        assert split_wheel_reports(data) == (data, "")

    def test_mixed_chunk(self) -> None:
        data = "hello\x1b[<64;10;20Mworld\x1b[<0;10;20M!"
        wheel, remaining = split_wheel_reports(data)
        assert wheel == "\x1b[<64;10;20M"
        assert remaining == "helloworld\x1b[<0;10;20M!"


# ─────────────────────────────────────────────────────────────────────
# AgentPane — locked input to composer filter (#728)
# ─────────────────────────────────────────────────────────────────────
class TestMoveLockedInputToComposer:
    def test_printable_text_inserted_into_composer(self) -> None:
        pane = AgentPane.__new__(AgentPane)
        pane.composer = MagicMock()
        cursor = MagicMock()
        pane.composer.editor.textCursor.return_value = cursor

        pane._move_locked_input_to_composer("hello")
        pane.composer.editor.setFocus.assert_called_once()
        pane.composer.editor.insertPlainText.assert_called_once_with("hello")

    def test_mouse_reports_and_escapes_ignored(self) -> None:
        pane = AgentPane.__new__(AgentPane)
        pane.composer = MagicMock()

        for escape_seq in ("\x1b[<0;10;20M", "\x1b[<64;10;20M", "\x1b[M`!!", "\x1b[A"):
            pane._move_locked_input_to_composer(escape_seq)
            pane.composer.editor.setFocus.assert_not_called()
            pane.composer.editor.insertPlainText.assert_not_called()
