"""Regression: a late processExited must not crash a torn-down pane.

The PtySession is parented to the engine, not the AgentPane, so it can outlive
the widget and deliver a queued ``processExited`` after Qt has begun destroying
the pane. Qt deletes child objects (like ``_tick_timer``) before the parent, so
``_on_exit`` -> ``set_state`` -> ``_tick_timer.stop()`` used to raise
``RuntimeError: wrapped C/C++ object of type QTimer has been deleted`` *inside a
Qt slot*, which makes PyQt abort the whole process (segfault).

``_on_exit`` now guards against this via ``sip.isdeleted``. These tests build a
real QFrame-backed pane, delete the C++ objects the crash touched, and assert
``_on_exit`` returns cleanly instead of raising.
"""

from __future__ import annotations

from PyQt6 import sip
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QFrame

from agent_takkub.agent_pane import AgentPane
from agent_takkub.agent_pane_model import AgentPaneModel
from agent_takkub.roles import by_name


def _bare_pane() -> AgentPane:
    """A pane with just enough real Qt state for _on_exit's teardown guard.

    Built via __new__ + QFrame.__init__ so the widget has a real C++ object
    (sip.isdeleted works) without paying for full AgentPane construction
    (TerminalWidget / QWebEngine). session/state live on self.model
    (issue #105 Phase A) — seed a bare model too.
    """
    pane = AgentPane.__new__(AgentPane)
    QFrame.__init__(pane)
    pane.model = AgentPaneModel(by_name("backend"))
    pane._tick_timer = QTimer(pane)
    pane._session_generation = 1
    pane.session = None
    return pane


def test_on_exit_survives_deleted_tick_timer() -> None:
    pane = _bare_pane()
    # Simulate Qt deleting the child timer before the parent during teardown.
    sip.delete(pane._tick_timer)
    assert sip.isdeleted(pane._tick_timer)

    # Must NOT raise RuntimeError (pre-fix this segfaulted the process).
    pane._on_exit(0, gen=1)


def test_on_exit_survives_deleted_pane() -> None:
    pane = _bare_pane()
    sip.delete(pane)
    assert sip.isdeleted(pane)

    # The Python wrapper is still referenced; a queued exit must no-op.
    pane._on_exit(0, gen=1)
