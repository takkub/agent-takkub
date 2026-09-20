"""#684: BacklogDialog construction + order-mode dispatch flow (headless).

A single Qt smoke test — the session-wide QApplication is provided by
tests/conftest.py's `_qt_session_app`. Visual layout is verified in the
running app; this pins that the dialog builds against the real orchestrator
entry point and that ตกลง hands the picked items to the LEAD in the picked
order (owner 2026-09-20: no role picker — the Lead sizes/routes itself).
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
            "_backlog_dispatch_to_lead",
            "_backlog_simple",
            "_backlog_result",
        ):
            setattr(self, name, getattr(Orchestrator, name).__get__(self))
        self._compose_backlog_task = Orchestrator._compose_backlog_task
        self._render_backlog_detail = Orchestrator._render_backlog_detail
        self.injected: list[str] = []
        self._pane_state: dict = {}

    @staticmethod
    def _resolve_project(project):
        return project or "default"

    def inject_lead_prompt(self, prompt: str, project=None) -> bool:
        self.injected.append(prompt)
        return True


def test_dialog_confirm_dispatches_to_lead_in_picked_order(runtime, _qt_session_app) -> None:
    from agent_takkub.backlog_dialog import BacklogDialog

    orch = _FakeOrch()
    a = backlog.add_item("proj", "งาน A")
    backlog.add_item("proj", "งาน B")
    c = backlog.add_item("proj", "งาน C")

    dlg = BacklogDialog(orch, "proj")
    try:
        assert dlg._list.count() == 3
        # turn on order mode, pick C then A
        dlg._order_cb.setChecked(True)
        dlg._picked = [c["id"], a["id"]]
        dlg._confirm()
        # ONE prompt handed to the Lead, items in picked order, telling the
        # Lead to route itself via `takkub backlog assign`
        assert len(orch.injected) == 1
        prompt = orch.injected[0]
        assert prompt.index("งาน C") < prompt.index("งาน A")
        assert "งาน B" not in prompt
        assert "takkub backlog assign" in prompt
        assert f"1. [{c['id']}]" in prompt and f"2. [{a['id']}]" in prompt
    finally:
        dlg.deleteLater()


def test_dialog_single_select_confirm(runtime, _qt_session_app) -> None:
    from agent_takkub.backlog_dialog import BacklogDialog

    orch = _FakeOrch()
    backlog.add_item("proj", "เดี่ยว")

    dlg = BacklogDialog(orch, "proj")
    try:
        dlg._list.setCurrentRow(0)
        dlg._confirm()
        assert len(orch.injected) == 1
        assert "เดี่ยว" in orch.injected[0]
    finally:
        dlg.deleteLater()


def test_dialog_confirm_with_nothing_selected_is_noop(runtime, _qt_session_app) -> None:
    from agent_takkub.backlog_dialog import BacklogDialog

    orch = _FakeOrch()
    dlg = BacklogDialog(orch, "proj")
    try:
        dlg._confirm()
        assert orch.injected == []
    finally:
        dlg.deleteLater()
