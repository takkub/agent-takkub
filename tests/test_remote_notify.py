"""Tests for `agent_takkub.remote.notify.LeadNotifier` (§6.5, X-check 2.1):
hooks `orch.agentDone` + `orch.statusChanged` -> Lead pane `bytesIn`, coalesces
raw PTY bytes before they reach the SSE broadcaster (finding B3).
"""

from __future__ import annotations

import time

import pytest
from PyQt6.QtCore import QCoreApplication, QObject, pyqtSignal

from agent_takkub.remote.notify import LeadNotifier


class _FakeSession(QObject):
    bytesIn = pyqtSignal(bytes)


class _FakePane:
    def __init__(self, session) -> None:
        self.session = session


class _FakeOrch(QObject):
    agentDone = pyqtSignal(str, str, str)
    statusChanged = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._panes_by_project = {"proj": {}}

    def _resolve_project(self, project):
        return "proj"


class _FakeBroadcaster:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def push(self, event: str, data: str) -> None:
        self.events.append((event, data))


@pytest.fixture
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


def _pump(qapp: QCoreApplication, ms: int = 250) -> None:
    deadline = time.time() + ms / 1000
    while time.time() < deadline:
        qapp.processEvents()


class TestDoneEvents:
    def test_agent_done_pushes_to_broadcaster(self, qapp):
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.agentDone.emit("proj", "backend", "added /auth/login")
            assert broadcaster.events == [("done", "backend: added /auth/login")]
        finally:
            notifier.stop()


class TestLeadOutputHook:
    def test_resyncs_to_lead_session_and_coalesces_bytes(self, qapp):
        orch = _FakeOrch()
        session = _FakeSession()
        orch._panes_by_project["proj"]["lead"] = _FakePane(session)
        broadcaster = _FakeBroadcaster()

        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.statusChanged.emit()  # picks up the newly-registered lead pane
            session.bytesIn.emit(b"hello ")
            session.bytesIn.emit(b"lead")
            _pump(qapp)
            assert broadcaster.events == [("lead", "hello lead")]
        finally:
            notifier.stop()

    def test_strips_ansi_escape_sequences(self, qapp):
        orch = _FakeOrch()
        session = _FakeSession()
        orch._panes_by_project["proj"]["lead"] = _FakePane(session)
        broadcaster = _FakeBroadcaster()

        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.statusChanged.emit()
            session.bytesIn.emit(b"\x1b[31mred text\x1b[0m")
            _pump(qapp)
            assert broadcaster.events == [("lead", "red text")]
        finally:
            notifier.stop()

    def test_switching_lead_session_disconnects_the_old_one(self, qapp):
        orch = _FakeOrch()
        old_session = _FakeSession()
        orch._panes_by_project["proj"]["lead"] = _FakePane(old_session)
        broadcaster = _FakeBroadcaster()

        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.statusChanged.emit()
            new_session = _FakeSession()
            orch._panes_by_project["proj"]["lead"] = _FakePane(new_session)
            orch.statusChanged.emit()  # e.g. respawn — a new session object

            old_session.bytesIn.emit(b"stale output")
            _pump(qapp)
            assert broadcaster.events == [], "old session must be disconnected"

            new_session.bytesIn.emit(b"fresh output")
            _pump(qapp)
            assert broadcaster.events == [("lead", "fresh output")]
        finally:
            notifier.stop()

    def test_no_lead_pane_is_a_safe_no_op(self, qapp):
        orch = _FakeOrch()  # no "lead" key registered
        broadcaster = _FakeBroadcaster()
        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.statusChanged.emit()
        finally:
            notifier.stop()
        assert broadcaster.events == []

    def test_stop_disconnects_everything(self, qapp):
        orch = _FakeOrch()
        session = _FakeSession()
        orch._panes_by_project["proj"]["lead"] = _FakePane(session)
        broadcaster = _FakeBroadcaster()
        notifier = LeadNotifier(orch, broadcaster)
        orch.statusChanged.emit()
        notifier.stop()

        session.bytesIn.emit(b"after stop")
        orch.agentDone.emit("proj", "backend", "note")
        _pump(qapp)
        assert broadcaster.events == []
