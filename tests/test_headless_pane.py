"""HeadlessPane (#105 Phase B) — display-free stand-in for AgentPane.

Verifies the data-only mirrors of AgentPane's view-mixed methods
(set_state, attach_session, detach_session, _on_exit) produce the same
state transitions the desktop AgentPane produces, with no QWidget anywhere.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.headless_pane import HeadlessPane
from agent_takkub.roles import LEAD, by_name


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _mock_session() -> MagicMock:
    """A PtySession stand-in with just the pyqtSignal-shaped attrs
    HeadlessPane.attach_session/detach_session touch."""
    session = MagicMock()
    session.bytesIn = MagicMock()
    session.processExited = MagicMock()
    session.processExited.connect.return_value = "exit-conn-handle"
    return session


def test_role_and_initial_state(qapp: QCoreApplication) -> None:
    pane = HeadlessPane(by_name("backend"))
    assert pane.role.name == "backend"
    assert pane.state == "empty"
    assert pane.session is None
    assert pane.last_note is None


def test_set_state_updates_model_only(qapp: QCoreApplication) -> None:
    pane = HeadlessPane(LEAD)
    pane.set_state("working", note="doing a thing")
    assert pane.state == "working"
    assert pane.last_note == "doing a thing"
    # note=None must NOT clear an existing note (matches AgentPane.set_state)
    pane.set_state("done")
    assert pane.state == "done"
    assert pane.last_note == "doing a thing"


def test_attach_session_binds_session_and_marks_active(qapp: QCoreApplication) -> None:
    pane = HeadlessPane(by_name("backend"))
    session = _mock_session()

    pane.attach_session(session, cwd="/tmp/project")

    assert pane.session is session
    assert pane.state == "active"
    assert pane._session_cwd == "/tmp/project"
    assert pane._session_generation == 1
    session.bytesIn.connect.assert_called_once_with(pane._mark_output_ts)
    session.processExited.connect.assert_called_once()


def test_mark_output_ts_bumps_last_output_ts(qapp: QCoreApplication) -> None:
    pane = HeadlessPane(by_name("backend"))
    pane._last_output_ts = 0.0
    pane._mark_output_ts(b"hello")
    assert pane._last_output_ts > 0.0


def test_detach_session_clears_session_and_disconnects(qapp: QCoreApplication) -> None:
    pane = HeadlessPane(by_name("backend"))
    session = _mock_session()
    pane.attach_session(session, cwd="/tmp/project")

    pane.detach_session()

    assert pane.session is None
    session.bytesIn.disconnect.assert_called_once_with(pane._mark_output_ts)
    session.processExited.disconnect.assert_called_once_with("exit-conn-handle")


def test_on_exit_expected_returns_to_empty(qapp: QCoreApplication) -> None:
    pane = HeadlessPane(by_name("backend"))
    session = _mock_session()
    pane.attach_session(session, cwd="/tmp/project")
    pane.mark_expected_exit()

    pane._on_exit(0, gen=pane._session_generation)

    assert pane.state == "empty"
    assert pane.session is None


def test_on_exit_unexpected_surfaces_exited(qapp: QCoreApplication) -> None:
    pane = HeadlessPane(by_name("backend"))
    session = _mock_session()
    pane.attach_session(session, cwd="/tmp/project")

    pane._on_exit(1, gen=pane._session_generation)

    assert pane.state == "exited"
    assert pane.last_note is not None
    assert "1" in pane.last_note


def test_on_exit_stale_generation_is_dropped(qapp: QCoreApplication) -> None:
    pane = HeadlessPane(by_name("backend"))
    session = _mock_session()
    pane.attach_session(session, cwd="/tmp/project")
    stale_gen = pane._session_generation

    # Re-attach a replacement session — bumps the generation.
    pane.attach_session(_mock_session(), cwd="/tmp/project")
    assert pane._session_generation != stale_gen

    pane._on_exit(1, gen=stale_gen)

    # The stale exit must be ignored — state stays "active" from the 2nd attach.
    assert pane.state == "active"


def test_current_usage_none_without_session(qapp: QCoreApplication) -> None:
    pane = HeadlessPane(by_name("backend"))
    assert pane.current_usage() is None


def test_set_worktree_branch(qapp: QCoreApplication) -> None:
    pane = HeadlessPane(by_name("backend"))
    pane.set_worktree_branch("wt/backend-1")
    assert pane._worktree_branch == "wt/backend-1"
    pane.set_worktree_branch(None)
    assert pane._worktree_branch is None


def test_signals_exist_for_register_pane_connect(qapp: QCoreApplication) -> None:
    """register_pane() connects spawnRequested/closeRequested/inputBytes —
    HeadlessPane must expose them even though headless mode never emits."""
    pane = HeadlessPane(by_name("backend"))
    received = []
    pane.closeRequested.connect(received.append)
    pane.closeRequested.emit("backend")
    assert received == ["backend"]
