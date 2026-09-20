"""#684: BacklogDialog construction + order-mode assign flow (headless).

A single Qt smoke test — the session-wide QApplication is provided by
tests/conftest.py's `_qt_session_app`. Visual layout is verified in the
running app; this pins that the dialog builds against the real orchestrator
entry point and that ตกลง fires assigns in the picked order.
"""

from __future__ import annotations

import pathlib

import pytest

from agent_takkub import backlog


@pytest.fixture
def runtime(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    monkeypatch.setattr(backlog, "RUNTIME_DIR", tmp_path / "runtime")
    return tmp_path


class _FakeOrch:
    def __init__(self) -> None:
        from agent_takkub.orchestrator import Orchestrator

        for name in (
            "backlog_command",
            "_backlog_assign",
            "_backlog_simple",
            "_backlog_result",
        ):
            setattr(self, name, getattr(Orchestrator, name).__get__(self))
        self._compose_backlog_task = Orchestrator._compose_backlog_task
        self._render_backlog_detail = Orchestrator._render_backlog_detail
        self.assign_calls: list[str] = []
        self._pane_state: dict = {}

    @staticmethod
    def _resolve_project(project):
        return project or "default"

    def assign(self, role, cwd, task, project=None, feature=""):
        from agent_takkub.orchestrator import _exit_key
        from agent_takkub.spawn_engine import PaneState

        # capture which title was assigned, in call order
        self.assign_calls.append(task.splitlines()[0])
        ps = PaneState()
        ps.task_id = f"t{len(self.assign_calls)}"
        self._pane_state[_exit_key(project or "default", role)] = ps
        return True, "ok"


def test_dialog_confirm_fires_assigns_in_picked_order(runtime, _qt_session_app) -> None:
    from agent_takkub.backlog_dialog import BacklogDialog

    orch = _FakeOrch()
    a = backlog.add_item("proj", "งาน A")
    b = backlog.add_item("proj", "งาน B")
    c = backlog.add_item("proj", "งาน C")

    dlg = BacklogDialog(orch, "proj")
    try:
        assert dlg._list.count() == 3
        # turn on order mode, pick C then A
        dlg._order_cb.setChecked(True)
        dlg._picked = [c["id"], a["id"]]
        dlg._role_combo.setCurrentText("frontend")
        dlg._confirm()
        # assigns fired in picked order: C, then A (B untouched)
        assert orch.assign_calls == ["งาน C", "งาน A"]
        assert backlog.get_item("proj", c["id"])["status"] == "doing"
        assert backlog.get_item("proj", b["id"])["status"] == "todo"
    finally:
        dlg.deleteLater()


def test_dialog_single_select_confirm(runtime, _qt_session_app) -> None:
    from agent_takkub.backlog_dialog import BacklogDialog

    orch = _FakeOrch()
    backlog.add_item("proj", "เดี่ยว")

    dlg = BacklogDialog(orch, "proj")
    try:
        dlg._list.setCurrentRow(0)
        dlg._role_combo.setCurrentText("backend")
        dlg._confirm()
        assert orch.assign_calls == ["เดี่ยว"]
    finally:
        dlg.deleteLater()
