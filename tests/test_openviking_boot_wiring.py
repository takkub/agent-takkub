"""main_window._boot()/closeEvent wiring for the OpenViking managed
runtime (Wave 2, `12_PERFORMANCE.md`): boot always reaps an orphaned
process and only conditionally starts one; close always stops an owned
one. Same stub-`MainWindow` harness `test_remote_scaffold.py`'s
`TestBootWiring` and `test_close_event_remote_stop.py` already use for the
analogous remote-control wiring — no real Qt window, no real subprocess."""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

import agent_takkub.main_window as mw_mod
from agent_takkub.openviking import manager as ov_manager_mod


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _make_boot_window_stub(monkeypatch) -> mw_mod.MainWindow:
    with patch.object(mw_mod.MainWindow, "__init__", lambda self: None):
        win = mw_mod.MainWindow.__new__(mw_mod.MainWindow)

    win._status = MagicMock()
    win.cli = MagicMock()
    win.cli.listen.return_value = 54321
    win.orch = MagicMock()
    win.orch.paneRequested = MagicMock()
    win.orch.paneRequested.connect = MagicMock()
    win._lead_first_input_fired = set()

    fake_lead = MagicMock()
    monkeypatch.setattr(mw_mod.MainWindow, "lead_pane", property(lambda self: fake_lead))
    monkeypatch.setattr(mw_mod, "active_project", lambda: ("test-project", None))
    monkeypatch.setattr(mw_mod, "preset_roles_for_active", lambda: [])
    monkeypatch.setattr(mw_mod, "get_open_tabs", lambda: [])
    monkeypatch.setattr(mw_mod.MainWindow, "_refresh_rtk_button", lambda self: None)
    monkeypatch.setattr(mw_mod.MainWindow, "_restore_teammates_from_snapshot", lambda self: None)
    monkeypatch.setattr(mw_mod.MainWindow, "_open_projects", lambda self: [])
    monkeypatch.setattr(mw_mod.MainWindow, "_persist_open_tabs", lambda self: None)

    # Remote-control wiring is orthogonal to this test — skip it cleanly
    # the same way `test_remote_scaffold.py`'s own boot tests do.
    orig_import_module = importlib.import_module

    def _skip_remote(name, *a, **kw):
        if name == "agent_takkub.remote":
            raise ModuleNotFoundError(name)
        return orig_import_module(name, *a, **kw)

    monkeypatch.setattr(importlib, "import_module", _skip_remote)
    return win


class TestBootWiringCallsOpenViking:
    def test_boot_calls_openviking_boot_wiring(self, qapp, monkeypatch) -> None:
        win = _make_boot_window_stub(monkeypatch)
        calls = []
        monkeypatch.setattr(ov_manager_mod, "boot_wiring", lambda: calls.append(1))

        win._boot()

        assert calls == [1]

    def test_boot_swallows_openviking_boot_wiring_exception(self, qapp, monkeypatch) -> None:
        win = _make_boot_window_stub(monkeypatch)

        def _boom():
            raise RuntimeError("boom")

        monkeypatch.setattr(ov_manager_mod, "boot_wiring", _boom)

        win._boot()  # must not raise


def _make_close_window_stub() -> mw_mod.MainWindow:
    with patch.object(mw_mod.MainWindow, "__init__", lambda self: None):
        win = mw_mod.MainWindow.__new__(mw_mod.MainWindow)

    win.orch = MagicMock()
    win.orch._panes_by_project = {}
    win.orch.write_session_snapshot = MagicMock()
    win.orch.write_resume_briefs = MagicMock()
    win._open_projects = MagicMock(return_value=["proj"])  # single tab: no confirm dialog
    win._save_window_state = MagicMock()
    win._persist_open_tabs = MagicMock()
    win._limit_store = None
    win.cli = MagicMock()
    win._remote = None
    return win


class TestCloseEventStopsOpenViking:
    def test_close_event_calls_openviking_stop(self, qapp: QCoreApplication, monkeypatch) -> None:
        win = _make_close_window_stub()
        fake = MagicMock()
        monkeypatch.setattr(ov_manager_mod, "get_manager", lambda: fake)

        event = MagicMock()
        with patch("agent_takkub.main_window.QMainWindow.closeEvent") as super_close:
            mw_mod.MainWindow.closeEvent(win, event)

        fake.stop.assert_called_once()
        super_close.assert_called_once()

    def test_close_event_swallows_openviking_stop_exception(
        self, qapp: QCoreApplication, monkeypatch
    ) -> None:
        win = _make_close_window_stub()
        fake = MagicMock()
        fake.stop.side_effect = RuntimeError("boom")
        monkeypatch.setattr(ov_manager_mod, "get_manager", lambda: fake)

        event = MagicMock()
        with patch("agent_takkub.main_window.QMainWindow.closeEvent") as super_close:
            mw_mod.MainWindow.closeEvent(win, event)

        fake.stop.assert_called_once()
        win.cli.close.assert_called_once()
        super_close.assert_called_once()
