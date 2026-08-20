"""Tests for `Orchestrator.answer_picker` (Remote mobile
AskUserQuestion fix). Writes a raw key sequence straight into the Lead
pane's PTY, bypassing `send()`'s chat-message pipeline entirely — see
docs/audit/2026-08-20-remote-askuserquestion.md for why that pipeline
cannot drive the picker (typed text is silently discarded; Enter alone
submits whatever option was already highlighted).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator
from agent_takkub.roles import LEAD


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _make_alive_session() -> MagicMock:
    s = MagicMock()
    s.is_alive = True
    s.write = MagicMock()
    return s


def _make_pane(session=None) -> MagicMock:
    p = MagicMock()
    p.session = session
    return p


@pytest.fixture
def orch(qapp, tmp_path, monkeypatch) -> Orchestrator:
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
    monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)

    with (
        patch.object(Orchestrator, "_start_hot_md_timer", lambda self: None, create=True),
        patch("agent_takkub.orchestrator.Orchestrator._load_pending_cc", lambda self: None),
        patch(
            "agent_takkub.orchestrator.Orchestrator._start_browser_mcps",
            lambda self: None,
            create=True,
        ),
    ):
        o = Orchestrator.__new__(Orchestrator)
        from PyQt6.QtCore import QObject

        QObject.__init__(o)
        o._panes_by_project = {}
        o._pane_state = {}
        o._idle_state = {}
        o._recent_exits = {}
        o._recent_done = []
        o._pending_lead_cc = {}
    return o


class TestAnswerPicker:
    def test_writes_raw_key_sequence_to_lead_pane(self, orch: Orchestrator) -> None:
        pane = _make_pane(session=_make_alive_session())
        orch._panes_by_project.setdefault("p", {})[LEAD.name] = pane
        ok, msg = orch.answer_picker("2", project="p")
        assert ok is True
        assert msg == "ok"
        pane.session.write.assert_called_once_with("2")

    def test_never_sanitizes_or_wraps_the_sequence(self, orch: Orchestrator) -> None:
        # Unlike send(), no header/paste-bracket/control-char stripping --
        # the caller (api._build_picker_key_sequence) already built exactly
        # the bytes the terminal needs.
        pane = _make_pane(session=_make_alive_session())
        orch._panes_by_project.setdefault("p", {})[LEAD.name] = pane
        orch.answer_picker("1\r", project="p")
        pane.session.write.assert_called_once_with("1\r")

    def test_empty_sequence_rejected(self, orch: Orchestrator) -> None:
        pane = _make_pane(session=_make_alive_session())
        orch._panes_by_project.setdefault("p", {})[LEAD.name] = pane
        ok, msg = orch.answer_picker("", project="p")
        assert ok is False
        assert "empty" in msg
        pane.session.write.assert_not_called()

    def test_no_lead_pane_open_fails_cleanly(self, orch: Orchestrator) -> None:
        ok, msg = orch.answer_picker("1", project="p")
        assert ok is False
        assert "not running" in msg

    def test_dead_session_fails_cleanly(self, orch: Orchestrator) -> None:
        pane = _make_pane(session=None)
        orch._panes_by_project.setdefault("p", {})[LEAD.name] = pane
        ok, msg = orch.answer_picker("1", project="p")
        assert ok is False
        assert "not running" in msg

    def test_only_ever_targets_the_lead_pane_not_teammates(self, orch: Orchestrator) -> None:
        # There is no such thing as a teammate-pane picker (#103,
        # spawn_engine.py denies teammates the AskUserQuestion tool) -- a
        # teammate pane present under the same project must never receive
        # these keystrokes even if somehow addressed.
        lead_pane = _make_pane(session=_make_alive_session())
        teammate_pane = _make_pane(session=_make_alive_session())
        orch._panes_by_project.setdefault("p", {})[LEAD.name] = lead_pane
        orch._panes_by_project["p"]["backend"] = teammate_pane
        orch.answer_picker("1", project="p")
        lead_pane.session.write.assert_called_once_with("1")
        teammate_pane.session.write.assert_not_called()
