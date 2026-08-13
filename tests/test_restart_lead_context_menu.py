"""Tests for "🔄 Restart Lead" on a project tab's right-click menu.

Covers `_on_restart_lead_from_menu` (confirm dialog + inactive-tab switch +
delegation to `_restart_lead_for_active_project`) — the handler wired up by
`_on_tab_context_menu`. See `test_restart_cockpit.py` for the sibling
full-cockpit-restart flow this mirrors.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtWidgets import QMessageBox as _RealQMessageBox

import agent_takkub.main_window as mw_mod

_OK = _RealQMessageBox.StandardButton.Ok
_CANCEL = _RealQMessageBox.StandardButton.Cancel


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _make_window_stub() -> mw_mod.MainWindow:
    win = mw_mod.MainWindow.__new__(mw_mod.MainWindow)
    win.orch = MagicMock()
    win.orch._project_panes.return_value = {}
    win.tabs = MagicMock()
    win.tabs.currentIndex.return_value = 0
    win._restart_lead_for_active_project = MagicMock()
    return win


def _mock_confirm(monkeypatch: pytest.MonkeyPatch, answer) -> MagicMock:
    q = MagicMock(return_value=answer)
    monkeypatch.setattr(mw_mod.QMessageBox, "question", q)
    return q


def test_confirm_ok_on_inactive_tab_switches_then_restarts(
    qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    win = _make_window_stub()
    win.tabs.currentIndex.return_value = 0
    _mock_confirm(monkeypatch, _OK)

    win._on_restart_lead_from_menu("other-proj", 2)

    win.tabs.setCurrentIndex.assert_called_once_with(2)
    win._restart_lead_for_active_project.assert_called_once_with()


def test_confirm_ok_on_already_active_tab_skips_switch(
    qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    win = _make_window_stub()
    win.tabs.currentIndex.return_value = 1
    _mock_confirm(monkeypatch, _OK)

    win._on_restart_lead_from_menu("proj", 1)

    win.tabs.setCurrentIndex.assert_not_called()
    win._restart_lead_for_active_project.assert_called_once_with()


def test_cancel_does_not_switch_or_restart(
    qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    win = _make_window_stub()
    win.tabs.currentIndex.return_value = 0
    _mock_confirm(monkeypatch, _CANCEL)

    win._on_restart_lead_from_menu("other-proj", 2)

    win.tabs.setCurrentIndex.assert_not_called()
    win._restart_lead_for_active_project.assert_not_called()


def test_working_panes_are_counted_from_the_target_project_only(
    qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    win = _make_window_stub()
    working_pane = MagicMock()
    working_pane.state = "working"
    idle_pane = MagicMock()
    idle_pane.state = "idle"
    win.orch._project_panes.return_value = {"backend": working_pane, "qa": idle_pane}
    confirm = _mock_confirm(monkeypatch, _CANCEL)

    win._on_restart_lead_from_menu("other-proj", 2)

    win.orch._project_panes.assert_called_once_with("other-proj")
    body = confirm.call_args.args[2]
    assert "1 pane(s)" in body
