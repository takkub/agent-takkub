"""Tests for issue #273's delivery-vs-task failure classification.

A pane that couldn't open the `_task_handoff_pointer` file-pointer handoff
never started the assigned WORK at all — `is_delivery_pointer_failure`
(orchestrator_text.py) recognizes that report and `Orchestrator.done()`
routes it to `_build_delivery_pointer_failure_notice` instead of the normal
fix-loop-propose wording, and skips the `role_memory.append_failure_entry`
capture (which would otherwise poison this role's future-spawn context with
a failure that was never about anything it did).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import LEAD, Orchestrator, PaneState, _exit_key
from agent_takkub.orchestrator_text import (
    DELIVERY_POINTER_FAILURE_WINDOW_SEC,
    is_delivery_pointer_failure,
)


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
    p.state = "working"
    p.set_state = MagicMock()
    return p


@pytest.fixture
def orch(qapp, tmp_path, monkeypatch) -> Orchestrator:
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
    monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)
    monkeypatch.setattr(orch_mod, "_resolve_vault_dir", lambda: None)
    monkeypatch.setattr(orch_mod, "active_project", lambda: ("proj", {}))

    from unittest.mock import patch

    with patch("agent_takkub.orchestrator.Orchestrator._load_pending_cc", lambda self: None):
        o = Orchestrator.__new__(Orchestrator)
        from PyQt6.QtCore import QObject

        QObject.__init__(o)
        o._panes_by_project = {}
        o._pane_state = {}
        o._idle_state = {}
        o._recent_exits = {}
        o._recent_done = []
        o._pending_lead_cc = {}
        o._pending_done_notices = {}
    monkeypatch.setattr(o, "_write_hot_md", MagicMock())
    return o


def _register_pane(orch: Orchestrator, role: str, project: str, session=None) -> MagicMock:
    pane = _make_pane(session)
    orch._panes_by_project.setdefault(project, {})[role] = pane
    return pane


_POINTER_ECHO_NOTE = "ไม่มี file-read tool ใน pane นี้ จึงเปิด task spec ตามข้อห้าม shell ไม่ได้"


class TestIsDeliveryPointerFailure:
    def test_true_when_pointer_used_and_wording_matches_and_fast(self) -> None:
        assert is_delivery_pointer_failure(_POINTER_ECHO_NOTE, "C:/x/task.md", 5.0) is True

    def test_false_without_task_file(self) -> None:
        # No pointer was ever sent for this assign — can't be a pointer failure.
        assert is_delivery_pointer_failure(_POINTER_ECHO_NOTE, None, 5.0) is False

    def test_false_when_outside_the_time_window(self) -> None:
        # Real work happening this long after assign is plausible; a report
        # this late is not "never even started".
        elapsed = DELIVERY_POINTER_FAILURE_WINDOW_SEC + 1.0
        assert is_delivery_pointer_failure(_POINTER_ECHO_NOTE, "C:/x/task.md", elapsed) is False

    def test_false_without_the_wording(self) -> None:
        # Fast + pointer-delivered but a genuinely different failure reason
        # — must not be misclassified just because it was quick.
        assert (
            is_delivery_pointer_failure("tests failed: 3 assertions", "C:/x/task.md", 5.0) is False
        )

    def test_english_echo_also_matches(self) -> None:
        assert (
            is_delivery_pointer_failure("no file-read tool available in this pane", "x.md", 1.0)
            is True
        )

    def test_empty_note_is_false(self) -> None:
        assert is_delivery_pointer_failure("", "C:/x/task.md", 1.0) is False
        assert is_delivery_pointer_failure(None, "C:/x/task.md", 1.0) is False


class TestDoneClassifiesDeliveryPointerFailure:
    def test_delivery_failure_gets_dedicated_notice_not_fix_loop(self, orch, tmp_path) -> None:
        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "frontend", proj, _make_alive_session())
        key = _exit_key(proj, "frontend")
        orch._pane_state[key] = PaneState(
            last_assigned_task_file="C:/handoff/task.md", assign_ts=__import__("time").time()
        )

        captured: list[str] = []
        orch._notify_lead = lambda ns, notice, **kw: captured.append(notice)  # type: ignore[assignment]

        orch.done("frontend", note=_POINTER_ECHO_NOTE, project=proj, failed=True)

        assert captured
        notice = captured[0]
        assert "delivery-failed" in notice
        assert "delivery failure" in notice
        # Must NOT be the fix-loop-propose wording — there's no work to
        # root-cause yet.
        assert "เสนอ fix loop" not in notice
        assert "Propose assign role ที่ทำงานนั้นให้แก้" not in notice

    def test_delivery_failure_skips_role_memory_capture(self, orch, tmp_path, monkeypatch) -> None:
        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "frontend", proj, _make_alive_session())
        key = _exit_key(proj, "frontend")
        orch._pane_state[key] = PaneState(
            last_assigned_task_file="C:/handoff/task.md", assign_ts=__import__("time").time()
        )
        orch._notify_lead = MagicMock()

        capture_mock = MagicMock()
        monkeypatch.setattr("agent_takkub.role_memory.append_failure_entry", capture_mock)

        orch.done("frontend", note=_POINTER_ECHO_NOTE, project=proj, failed=True)

        capture_mock.assert_not_called()

    def test_genuine_task_failure_still_gets_fix_loop_wording(self, orch, tmp_path) -> None:
        # No task_file (short task, pasted inline) — normal classification.
        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "qa", proj, _make_alive_session())

        captured: list[str] = []
        orch._notify_lead = lambda ns, notice, **kw: captured.append(notice)  # type: ignore[assignment]

        orch.done("qa", note="login test failed: 500 on /auth", project=proj, failed=True)

        assert captured
        notice = captured[0]
        assert "FAILED" in notice
        assert "เสนอ fix loop" in notice
        assert "delivery-failed" not in notice

    def test_genuine_task_failure_still_captured_in_role_memory(
        self, orch, tmp_path, monkeypatch
    ) -> None:
        proj = "proj"
        _register_pane(orch, LEAD.name, proj, _make_alive_session())
        _register_pane(orch, "qa", proj, _make_alive_session())
        orch._notify_lead = MagicMock()

        capture_mock = MagicMock()
        monkeypatch.setattr("agent_takkub.role_memory.append_failure_entry", capture_mock)

        orch.done("qa", note="login test failed: 500 on /auth", project=proj, failed=True)

        capture_mock.assert_called_once()
