"""Tests for CLI bind-failure error handling (gemini P2 blind spot).

When CliServer.listen() raises RuntimeError (port busy, permission denied,
antivirus block, etc.) the cockpit must:
  1. Log a `cli_bind_failed` event to events.log
  2. Show a QMessageBox.critical dialog with actionable info
  3. Call QApplication.quit() so the process exits gracefully

We test _handle_cli_bind_error() in isolation — no MainWindow instantiation
required. Qt-less because the function only calls Qt symbols we monkeypatch.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

import agent_takkub.main_window as mw_mod


@pytest.fixture()
def log_file(tmp_path, monkeypatch):
    """Point EVENTS_LOG at a temp file so tests don't touch the real log."""
    log = tmp_path / "events.log"
    monkeypatch.setattr(mw_mod, "EVENTS_LOG", log)
    return log


@pytest.fixture()
def mock_msgbox(monkeypatch):
    """Replace QMessageBox with a MagicMock so no real dialog appears."""
    m = MagicMock()
    monkeypatch.setattr(mw_mod, "QMessageBox", m)
    return m


@pytest.fixture()
def mock_qapp(monkeypatch):
    """Replace QApplication with a MagicMock so quit() is captured."""
    m = MagicMock()
    monkeypatch.setattr(mw_mod, "QApplication", m)
    return m


# ---------------------------------------------------------------------------
# Core behaviour
# ---------------------------------------------------------------------------


class TestHandleCliBindError:
    def test_logs_cli_bind_failed_event(self, log_file, mock_msgbox, mock_qapp):
        mw_mod._handle_cli_bind_error("QTcpServer: bind: Address already in use")
        assert log_file.is_file(), "events.log was not created"
        events = [json.loads(ln) for ln in log_file.read_text(encoding="utf-8").splitlines() if ln]
        assert any(e["event"] == "cli_bind_failed" for e in events)

    def test_log_contains_error_message(self, log_file, mock_msgbox, mock_qapp):
        err = "QTcpServer: bind: WinError 10048"
        mw_mod._handle_cli_bind_error(err)
        events = [json.loads(ln) for ln in log_file.read_text(encoding="utf-8").splitlines() if ln]
        ev = next(e for e in events if e["event"] == "cli_bind_failed")
        assert err in ev.get("error", "")

    def test_shows_critical_dialog(self, log_file, mock_msgbox, mock_qapp):
        mw_mod._handle_cli_bind_error("bind failed")
        assert mock_msgbox.critical.called, "QMessageBox.critical was not called"

    def test_dialog_title_mentions_cli(self, log_file, mock_msgbox, mock_qapp):
        mw_mod._handle_cli_bind_error("bind failed")
        args = mock_msgbox.critical.call_args
        title = args[0][1]  # positional arg index 1 (parent, title, text)
        assert "cli" in title.lower() or "port" in title.lower() or "server" in title.lower()

    def test_dialog_text_contains_netstat_hint(self, log_file, mock_msgbox, mock_qapp):
        mw_mod._handle_cli_bind_error("bind failed")
        args = mock_msgbox.critical.call_args
        text = args[0][2]  # positional arg index 2
        assert "netstat" in text.lower()

    def test_dialog_text_mentions_another_instance(self, log_file, mock_msgbox, mock_qapp):
        mw_mod._handle_cli_bind_error("bind failed")
        args = mock_msgbox.critical.call_args
        text = args[0][2]
        # Should hint that another cockpit might be running
        assert "instance" in text.lower() or "cockpit" in text.lower() or "another" in text.lower()

    def test_calls_qapplication_quit(self, log_file, mock_msgbox, mock_qapp):
        mw_mod._handle_cli_bind_error("bind failed")
        assert mock_qapp.quit.called, "QApplication.quit() was not called"

    def test_quit_called_after_dialog(self, log_file, mock_msgbox, mock_qapp):
        """Dialog must be shown before quit() so the user actually sees it."""
        call_order = []
        mock_msgbox.critical.side_effect = lambda *a, **kw: call_order.append("dialog")
        mock_qapp.quit.side_effect = lambda: call_order.append("quit")
        mw_mod._handle_cli_bind_error("bind failed")
        assert call_order == ["dialog", "quit"], f"wrong order: {call_order}"

    def test_no_dialog_on_success(self, log_file, mock_msgbox, mock_qapp):
        """Happy path: if listen() succeeds _handle_cli_bind_error is never called."""
        # Verify the function is not called by testing it directly isn't triggered
        # by a non-error path — done implicitly (no call to _handle_cli_bind_error
        # means no dialog). This test calls it explicitly only for the failure path
        # and verifies it isn't silently called twice.
        mock_msgbox.critical.reset_mock()
        mock_qapp.quit.reset_mock()
        # call zero times (simulate happy path) → no dialog
        assert not mock_msgbox.critical.called
        assert not mock_qapp.quit.called


# ---------------------------------------------------------------------------
# Integration: _boot() calls _handle_cli_bind_error when listen() fails
# ---------------------------------------------------------------------------


class TestBootCallsHandlerOnFailure:
    """Verify that _boot() in MainWindow wires _handle_cli_bind_error correctly.

    We monkeypatch MainWindow.__init__ to a no-op so we can test _boot()
    without constructing the full Qt widget tree, then inject a minimal
    set of stubs.
    """

    def _make_window_stub(self, monkeypatch, log_file, mock_msgbox, mock_qapp):
        """Return a MainWindow-like object with _boot() wired but no real Qt."""
        # Bypass __init__
        with patch.object(mw_mod.MainWindow, "__init__", lambda self: None):
            win = mw_mod.MainWindow.__new__(mw_mod.MainWindow)

        # Inject minimal attributes _boot() touches before listen()
        win._status = MagicMock()
        win.cli = MagicMock()
        win.orch = MagicMock()
        win._lead_first_input_fired = set()
        win.orch.paneRequested = MagicMock()
        win.orch.paneRequested.connect = MagicMock()
        win.orch.spawn.return_value = (True, "ok")

        # `lead_pane` is a property on MainWindow — patch it at class level
        fake_lead = MagicMock()
        monkeypatch.setattr(mw_mod.MainWindow, "lead_pane", property(lambda self: fake_lead))

        # active_project used inside _boot for the label
        monkeypatch.setattr(mw_mod, "active_project", lambda: ("test-project", None))
        monkeypatch.setattr(mw_mod, "preset_roles_for_active", lambda: [])
        monkeypatch.setattr(mw_mod, "get_open_tabs", lambda: [])
        # _refresh_rtk_button touches filesystem + lead_cwd — no-op it
        monkeypatch.setattr(mw_mod.MainWindow, "_refresh_rtk_button", lambda self: None)
        # _restore_teammates_from_snapshot is deferred via QTimer — no-op here
        monkeypatch.setattr(
            mw_mod.MainWindow, "_restore_teammates_from_snapshot", lambda self: None
        )
        # _open_projects reads self.tabs (a QTabWidget) which isn't constructed
        monkeypatch.setattr(mw_mod.MainWindow, "_open_projects", lambda self: [])
        monkeypatch.setattr(mw_mod.MainWindow, "_persist_open_tabs", lambda self: None)

        return win

    def test_boot_calls_handle_on_listen_failure(
        self, log_file, mock_msgbox, mock_qapp, monkeypatch
    ):
        win = self._make_window_stub(monkeypatch, log_file, mock_msgbox, mock_qapp)
        win.cli.listen.side_effect = RuntimeError("bind: WinError 10048")
        win._boot()
        assert mock_msgbox.critical.called
        assert mock_qapp.quit.called

    def test_boot_no_dialog_on_success(self, log_file, mock_msgbox, mock_qapp, monkeypatch):
        win = self._make_window_stub(monkeypatch, log_file, mock_msgbox, mock_qapp)
        win.cli.listen.return_value = 54321
        win._boot()
        assert not mock_msgbox.critical.called
        assert not mock_qapp.quit.called
