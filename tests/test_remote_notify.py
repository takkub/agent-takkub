"""Tests for `agent_takkub.remote.notify.LeadNotifier` (§6.5, X-check 2.1):
hooks `orch.agentDone` + `orch.statusChanged` -> Lead pane `bytesIn`, coalesces
raw PTY bytes before they reach the SSE broadcaster (finding B3).
"""

from __future__ import annotations

import time

import pytest
from PyQt6.QtCore import QCoreApplication, QObject, pyqtSignal

from agent_takkub.remote.notify import LeadNotifier, _filter_junk


class _FakeSession(QObject):
    bytesIn = pyqtSignal(bytes)


class _FakePane:
    def __init__(self, session) -> None:
        self.session = session


class _FakeOrch(QObject):
    agentDone = pyqtSignal(str, str, str)
    statusChanged = pyqtSignal()

    def __init__(self, active_project: str = "proj") -> None:
        super().__init__()
        self._panes_by_project = {"proj": {}}
        self._active_project = active_project

    def _resolve_project(self, project):
        return self._active_project


class _FakeBroadcaster:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str | None]] = []

    def push(self, event: str, data: str, project_ns: str | None = None) -> None:
        self.events.append((event, data, project_ns))


@pytest.fixture
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


def _pump(qapp: QCoreApplication, ms: int = 250) -> None:
    deadline = time.time() + ms / 1000
    while time.time() < deadline:
        qapp.processEvents()


class TestFilterJunk:
    def test_keeps_plain_conversation_text_untouched(self):
        assert _filter_junk("hello lead") == "hello lead"

    def test_drops_composer_box_and_spinner_status_but_keeps_reply(self):
        raw = (
            "╭──────────────────────────────────────────╮\n"
            "│ > add auth middleware                     │\n"
            "╰──────────────────────────────────────────╯\n"
            "\n"
            "✻ Pondering… (12s · 1.2k tokens · esc to interrupt)\n"
            "\n"
            "⏺ Read(src/auth.py)\n"
            "\n"
            "I added JWT-based auth middleware to src/auth.py.\n"
            "It validates the Bearer token and attaches the user to the request.\n"
            "\n"
            "  ⠋ Compacting context…\n"
            "\n"
            "? for shortcuts                    bypass permissions on (shift+tab to cycle)\n"
        )
        result = _filter_junk(raw)
        assert result == (
            "I added JWT-based auth middleware to src/auth.py.\n"
            "It validates the Bearer token and attaches the user to the request."
        )

    def test_drops_tool_call_xml_rendering(self):
        raw = (
            '<function_calls>\n<invoke name="Read">\n</invoke>\n</function_calls>\nreal reply here'
        )
        assert _filter_junk(raw) == "real reply here"

    def test_collapses_repeated_blank_lines_to_one(self):
        raw = "first line\n\n\n\nsecond line"
        assert _filter_junk(raw) == "first line\n\nsecond line"

    def test_drops_leading_and_trailing_blank_lines(self):
        raw = "\n\n  \nactual content\n\n\n"
        assert _filter_junk(raw) == "actual content"

    def test_empty_or_pure_junk_input_yields_empty_string(self):
        assert _filter_junk("") == ""
        assert _filter_junk("──────\n✻ Thinking… (esc to interrupt)\n") == ""


class TestDoneEvents:
    def test_agent_done_pushes_to_broadcaster(self, qapp):
        orch = _FakeOrch()
        broadcaster = _FakeBroadcaster()
        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.agentDone.emit("proj", "backend", "added /auth/login")
            assert broadcaster.events == [("done", "backend: added /auth/login", "proj")]
        finally:
            notifier.stop()

    def test_done_from_a_different_project_is_stamped_with_its_own_namespace(self, qapp):
        # H-A: `agentDone` fires for every project, not just whichever one
        # is active — the notifier must forward the *event's* project, so
        # the broadcaster (not the notifier) is what keeps it from leaking
        # into a different project's SSE client.
        orch = _FakeOrch(active_project="proj")
        broadcaster = _FakeBroadcaster()
        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.agentDone.emit("other-proj", "backend", "did a thing")
            assert broadcaster.events == [("done", "backend: did a thing", "other-proj")]
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
            assert broadcaster.events == [("lead", "hello lead", "proj")]
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
            assert broadcaster.events == [("lead", "red text", "proj")]
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
            assert broadcaster.events == [("lead", "fresh output", "proj")]
        finally:
            notifier.stop()

    def test_project_switch_mid_coalesce_does_not_leak_across_projects(self, qapp):
        # B1 regression: `_resync_lead_session` used to stamp *all* buffered
        # bytes with whatever `self._project_ns` happens to be at flush time.
        # If the active project switches mid-coalesce (before the 150ms
        # timer fires), bytes captured while proj-a was active would be
        # flushed under proj-b's namespace — proj-b's mobile client would
        # see proj-a's live Lead output.
        orch = _FakeOrch(active_project="proj-a")
        session_a = _FakeSession()
        session_b = _FakeSession()
        orch._panes_by_project = {
            "proj-a": {"lead": _FakePane(session_a)},
            "proj-b": {"lead": _FakePane(session_b)},
        }
        broadcaster = _FakeBroadcaster()

        notifier = LeadNotifier(orch, broadcaster)
        try:
            orch.statusChanged.emit()  # resyncs to proj-a's lead pane
            session_a.bytesIn.emit(b"from proj-a")

            # switch the active project before the coalesce timer fires
            orch._active_project = "proj-b"
            orch.statusChanged.emit()
            session_b.bytesIn.emit(b"from proj-b")

            _pump(qapp)
            assert ("lead", "from proj-a", "proj-a") in broadcaster.events
            assert ("lead", "from proj-b", "proj-b") in broadcaster.events
            for _event, text, ns in broadcaster.events:
                if ns == "proj-b":
                    assert "proj-a" not in text
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
