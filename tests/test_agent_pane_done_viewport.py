"""#671: a "done" pane with a live session must keep showing its terminal.

Pane reuse (2.1.17) stopped closing panes after `takkub done`, but
`AgentPane.set_state` still flipped the body stack to the "empty slot"
placeholder for every non-active/working state — so a living, kept pane
rendered as an empty slot for its whole keep window while `takkub status`
saw the real screen buffer (field case 2026-09-18, backend claude/sonnet-5).

Same fake-TerminalWidget approach as test_agent_pane_auto_clear.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QApplication, QWidget

import agent_takkub.agent_pane as agent_pane_mod
from agent_takkub.agent_pane import AgentPane
from agent_takkub.roles import by_name


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _FakeTerminalWidget(QWidget):
    inputBytes = pyqtSignal(bytes)
    resized = pyqtSignal(int, int)
    fontSizeChanged = pyqtSignal(int)
    openInEditorRequested = pyqtSignal(str)

    def clear_view(self) -> None:
        pass

    def set_keepalive(self, active: bool) -> None:
        pass

    def set_input_locked(self, locked: bool) -> None:
        pass

    def set_cwd(self, cwd) -> None:
        pass

    def set_font_point_size(self, size: int) -> None:
        pass

    def write_bytes(self, data) -> None:
        pass

    def reset(self) -> None:
        pass

    def set_idle(self, idle: bool) -> None:
        pass

    def set_discard_enabled(self, enabled: bool) -> None:
        pass

    def set_discard_guard(self, guard) -> None:
        pass

    def setFocus(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _fake_terminal(monkeypatch):
    monkeypatch.setattr(agent_pane_mod, "TerminalWidget", _FakeTerminalWidget)


@pytest.fixture(autouse=True)
def _keep_mode(monkeypatch):
    # #671 is a keep-alive-mode behaviour ("a kept-alive done pane keeps its
    # terminal"). Close-on-done is now the default (#683), so pin keep mode
    # explicitly here.
    monkeypatch.setenv("TAKKUB_CLOSE_ON_DONE", "0")


def _make_pane() -> AgentPane:
    pane = AgentPane(by_name("backend"))
    pane._idle_clear_timer.stop()
    pane._done_clear_timer.stop()
    return pane


_TERMINAL_PAGE = 1
_PLACEHOLDER_PAGE = 0


class TestDoneViewport:
    def test_done_with_live_session_keeps_terminal_visible(self, qapp) -> None:
        pane = _make_pane()
        pane.session = MagicMock()
        pane.set_state("working")
        assert pane._stack.currentIndex() == _TERMINAL_PAGE

        pane.set_state("done", note="เสร็จแล้ว")

        assert pane._stack.currentIndex() == _TERMINAL_PAGE, (
            "a kept-alive done pane must render its buffer, not 'empty slot'"
        )
        # The pane is alive: export/zoom stay usable, respawn button hidden.
        assert pane._btn_spawn.isHidden()

    def test_done_without_session_shows_placeholder(self, qapp) -> None:
        # The process exited on its own after done (decide_exit_state ->
        # "empty" normally covers this, but a bare "done" with no session
        # must not pretend there is a buffer to show).
        pane = _make_pane()
        pane.session = None

        pane.set_state("done")

        assert pane._stack.currentIndex() == _PLACEHOLDER_PAGE

    def test_close_path_still_reaches_placeholder(self, qapp) -> None:
        # Orchestrator.close() sets "empty" while the session is still
        # attached (terminate is async) — placeholder is correct there.
        pane = _make_pane()
        pane.session = MagicMock()
        pane.set_state("working")

        pane.set_state("empty")

        assert pane._stack.currentIndex() == _PLACEHOLDER_PAGE

    def test_exited_state_shows_placeholder(self, qapp) -> None:
        pane = _make_pane()
        pane.session = MagicMock()
        pane.set_state("working")

        pane.set_state("exited", note="agent process exited unexpectedly (code 1)")

        assert pane._stack.currentIndex() == _PLACEHOLDER_PAGE
