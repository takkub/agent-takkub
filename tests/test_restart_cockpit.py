"""Tests for the Restart Cockpit button (user-triggered restart).

Covers _on_restart_cockpit_clicked (confirm dialog + audit log + delegation)
and the persistence improvements inside _restart_cockpit (state saved before
subprocess.Popen so data survives even if QCoreApplication.quit() skips
closeEvent).
"""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtWidgets import QMessageBox as _RealQMessageBox

import agent_takkub.main_window as mw_mod
import agent_takkub.update_panel as up_mod
from agent_takkub._restart_env import AUTO_PORT_FILE_ENV, configure_multi_instance_port_file

# Real enum values so comparisons inside _on_restart_cockpit_clicked work.
_OK = _RealQMessageBox.StandardButton.Ok
_CANCEL = _RealQMessageBox.StandardButton.Cancel


# ─────────────────────────────────────────────────────────────
# Shared fixtures
# ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _make_window_stub(monkeypatch: pytest.MonkeyPatch) -> mw_mod.MainWindow:
    """Return a MainWindow-like object without a real Qt widget tree."""
    with patch.object(mw_mod.MainWindow, "__init__", lambda self: None):
        win = mw_mod.MainWindow.__new__(mw_mod.MainWindow)

    win.orch = MagicMock()
    win.orch._panes_by_project = {}
    win._status = MagicMock()
    # Stub out the heavy persistence/launch helpers so tests stay fast.
    win._save_window_state = MagicMock()
    win._persist_open_tabs = MagicMock()
    win._restart_cockpit = MagicMock()
    return win


def _mock_msgbox(monkeypatch: pytest.MonkeyPatch, answer: _RealQMessageBox.StandardButton):
    """Replace QMessageBox in update_panel with a mock whose .question() returns `answer`."""
    m = MagicMock()
    m.question.return_value = answer
    m.StandardButton.Ok = _OK
    m.StandardButton.Cancel = _CANCEL
    monkeypatch.setattr(up_mod, "QMessageBox", m)
    return m


# ─────────────────────────────────────────────────────────────
# 1. Audit event
# ─────────────────────────────────────────────────────────────


class TestRestartEmitsAuditEvent:
    def test_restart_emits_audit_event(
        self, qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        win = _make_window_stub(monkeypatch)
        win.orch._panes_by_project = {}

        logged: list[dict] = []
        monkeypatch.setattr(
            up_mod, "_log_event", lambda event, **kw: logged.append({"event": event, **kw})
        )
        _mock_msgbox(monkeypatch, _OK)

        win._on_restart_cockpit_clicked()

        events = [e for e in logged if e["event"] == "cockpit_restart"]
        assert events, "_log_event('cockpit_restart') was never called"
        assert events[0]["reason"] == "user_action"

    def test_audit_event_includes_working_panes_count(
        self, qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        win = _make_window_stub(monkeypatch)
        working_pane = MagicMock()
        working_pane.state = "working"
        win.orch._panes_by_project = {"proj": {"backend": working_pane}}

        logged: list[dict] = []
        monkeypatch.setattr(
            up_mod, "_log_event", lambda event, **kw: logged.append({"event": event, **kw})
        )
        _mock_msgbox(monkeypatch, _OK)

        win._on_restart_cockpit_clicked()

        ev = next((e for e in logged if e["event"] == "cockpit_restart"), None)
        assert ev is not None
        assert ev["working_panes"] == 1


# ─────────────────────────────────────────────────────────────
# 2. Handler delegates to _restart_cockpit WITHOUT pre-closing panes
#    (snapshot must capture live panes — closing first empties snapshot)
# ─────────────────────────────────────────────────────────────


class TestRestartDelegatesToRestartCockpit:
    def test_restart_delegates_to_restart_cockpit(
        self, qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        win = _make_window_stub(monkeypatch)
        win.orch._panes_by_project = {
            "proj-a": {"lead": MagicMock(state="active"), "frontend": MagicMock(state="working")},
        }
        win.orch.close_all_teammates = MagicMock()
        win.orch.close = MagicMock(return_value=(True, "ok"))

        monkeypatch.setattr(up_mod, "_log_event", lambda *a, **kw: None)
        _mock_msgbox(monkeypatch, _OK)

        win._on_restart_cockpit_clicked()

        win._restart_cockpit.assert_called_once()

    def test_handler_does_not_close_panes_before_restart(
        self, qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """close_all_teammates must NOT be called by the handler — snapshot
        needs panes alive so _restart_cockpit can capture them."""
        win = _make_window_stub(monkeypatch)
        pane = MagicMock(state="working")
        win.orch._panes_by_project = {"proj-a": {"lead": pane}, "proj-b": {"lead": pane}}
        win.orch.close_all_teammates = MagicMock()
        win.orch.close = MagicMock(return_value=(True, "ok"))

        monkeypatch.setattr(up_mod, "_log_event", lambda *a, **kw: None)
        _mock_msgbox(monkeypatch, _OK)
        win._restart_cockpit = MagicMock()

        mw_mod.MainWindow._on_restart_cockpit_clicked(win)

        win.orch.close_all_teammates.assert_not_called()
        # close() must not be called with force=True from the handler either
        force_calls = [c for c in win.orch.close.call_args_list if c.kwargs.get("force") is True]
        assert force_calls == [], "handler must not force-close panes before _restart_cockpit"


# ─────────────────────────────────────────────────────────────
# 3. Cancel aborts restart
# ─────────────────────────────────────────────────────────────


class TestRestartConfirmCancels:
    def test_restart_confirm_cancels_when_user_says_no(
        self, qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        win = _make_window_stub(monkeypatch)
        win.orch._panes_by_project = {"proj": {"lead": MagicMock()}}
        win.orch.close_all_teammates = MagicMock()
        win.orch.close = MagicMock(return_value=(True, "ok"))

        logged: list = []
        monkeypatch.setattr(up_mod, "_log_event", lambda *a, **kw: logged.append(a))
        _mock_msgbox(monkeypatch, _CANCEL)

        win._on_restart_cockpit_clicked()

        win._restart_cockpit.assert_not_called()
        assert logged == [], "_log_event must not fire on cancel"

    def test_cancel_does_not_close_panes(
        self, qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        win = _make_window_stub(monkeypatch)
        win.orch._panes_by_project = {"proj": {"lead": MagicMock()}}
        win.orch.close_all_teammates = MagicMock()
        win.orch.close = MagicMock(return_value=(True, "ok"))

        monkeypatch.setattr(up_mod, "_log_event", lambda *a, **kw: None)
        _mock_msgbox(monkeypatch, _CANCEL)

        # Bind real method (the stub's _restart_cockpit is already mocked)
        mw_mod.MainWindow._on_restart_cockpit_clicked(win)

        win.orch.close_all_teammates.assert_not_called()
        win.orch.close.assert_not_called()


# ─────────────────────────────────────────────────────────────
# 4. No active panes — simpler confirm message
# ─────────────────────────────────────────────────────────────


class TestRestartNoActivePanes:
    def test_confirm_shown_even_with_no_active_panes(
        self, qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        win = _make_window_stub(monkeypatch)
        win.orch._panes_by_project = {}

        shown: list[str] = []

        def _capture(parent, title, text, *a, **kw):
            shown.append(text)
            return _OK

        monkeypatch.setattr(up_mod, "_log_event", lambda *a, **kw: None)
        m = MagicMock()
        m.question.side_effect = _capture
        m.StandardButton.Ok = _OK
        m.StandardButton.Cancel = _CANCEL
        monkeypatch.setattr(up_mod, "QMessageBox", m)

        win._on_restart_cockpit_clicked()

        assert shown, "confirm dialog was never displayed"
        win._restart_cockpit.assert_called_once()

    def test_no_active_panes_message_does_not_mention_working(
        self, qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        win = _make_window_stub(monkeypatch)
        win.orch._panes_by_project = {}

        shown: list[str] = []

        def _capture(parent, title, text, *a, **kw):
            shown.append(text)
            return _CANCEL  # cancel so we don't need _restart_cockpit

        monkeypatch.setattr(up_mod, "_log_event", lambda *a, **kw: None)
        m = MagicMock()
        m.question.side_effect = _capture
        m.StandardButton.Ok = _OK
        m.StandardButton.Cancel = _CANCEL
        monkeypatch.setattr(up_mod, "QMessageBox", m)

        mw_mod.MainWindow._on_restart_cockpit_clicked(win)

        assert shown
        assert "working" not in shown[0].lower() or "0" not in shown[0]


# ─────────────────────────────────────────────────────────────
# 5. PORT_FILE released before restart
# ─────────────────────────────────────────────────────────────


class TestRestartReleasesPortFile:
    def _make_restart_stub(self, tmp_path: pathlib.Path):
        """Minimal stub that exercises the real _restart_cockpit body."""
        with patch.object(mw_mod.MainWindow, "__init__", lambda self: None):
            win = mw_mod.MainWindow.__new__(mw_mod.MainWindow)
        win.orch = MagicMock()
        win._status = MagicMock()
        win._save_window_state = MagicMock()
        win._persist_open_tabs = MagicMock()
        return win

    def test_restart_releases_port_file(
        self,
        qapp: QCoreApplication,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: pathlib.Path,
    ) -> None:
        port_file = tmp_path / "port"
        port_file.write_text("12345", encoding="utf-8")
        # TAKKUB_PORT_FILE (the effective override) drives config._get_port_file(),
        # which _release_port_file() now uses instead of the static PORT_FILE
        # constant (see finding 5 in the isolation-plan crosscheck).
        monkeypatch.setenv("TAKKUB_PORT_FILE", str(port_file))
        monkeypatch.setattr(up_mod, "QCoreApplication", MagicMock())

        win = self._make_restart_stub(tmp_path)

        popen_calls: list = []

        def _fake_popen(cmd, **kw):
            popen_calls.append(cmd)
            # PORT_FILE must be deleted BEFORE Popen
            assert not port_file.exists(), "PORT_FILE must be deleted BEFORE Popen"
            return MagicMock()

        with patch("subprocess.Popen", side_effect=_fake_popen):
            mw_mod.MainWindow._restart_cockpit(win)

        assert popen_calls, "Popen was never called"
        assert not port_file.exists(), "PORT_FILE was not deleted by _restart_cockpit"

    def test_restart_persistence_order(
        self,
        qapp: QCoreApplication,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: pathlib.Path,
    ) -> None:
        """_save_window_state and write_session_snapshot must be called BEFORE Popen."""
        call_order: list[str] = []

        win = self._make_restart_stub(tmp_path)
        win._save_window_state = MagicMock(
            side_effect=lambda: call_order.append("save_window_state")
        )
        win._persist_open_tabs = MagicMock(
            side_effect=lambda: call_order.append("persist_open_tabs")
        )
        win.orch.write_session_snapshot = MagicMock(
            side_effect=lambda: call_order.append("write_session_snapshot")
        )
        win.orch.write_resume_briefs = MagicMock(
            side_effect=lambda: call_order.append("write_resume_briefs")
        )

        monkeypatch.setenv("TAKKUB_PORT_FILE", str(tmp_path / "port"))
        monkeypatch.setattr(up_mod, "QCoreApplication", MagicMock())

        def _fake_popen(cmd, **kw):
            call_order.append("popen")
            return MagicMock()

        with patch("subprocess.Popen", side_effect=_fake_popen):
            mw_mod.MainWindow._restart_cockpit(win)

        assert "save_window_state" in call_order
        assert "write_session_snapshot" in call_order
        popen_idx = call_order.index("popen")
        for step in ("save_window_state", "write_session_snapshot"):
            assert call_order.index(step) < popen_idx, f"{step} must happen before Popen"

    def test_successor_rederives_app_generated_multi_instance_port_file(
        self,
        qapp: QCoreApplication,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: pathlib.Path,
    ) -> None:
        """A per-PID path generated by app.py must not leak to the new PID."""
        port_file = tmp_path / "agent-takkub-port.111"
        port_file.write_text("12345", encoding="utf-8")
        monkeypatch.setenv("TAKKUB_PORT_FILE", str(port_file))
        monkeypatch.setenv(AUTO_PORT_FILE_ENV, str(port_file))
        monkeypatch.setattr(up_mod, "QCoreApplication", MagicMock())
        win = self._make_restart_stub(tmp_path)

        popen_kwargs: dict = {}

        def _fake_popen(cmd, **kw):
            popen_kwargs.update(kw)
            return MagicMock()

        with patch("subprocess.Popen", side_effect=_fake_popen):
            mw_mod.MainWindow._restart_cockpit(win)

        successor_env = popen_kwargs["env"]
        assert "TAKKUB_PORT_FILE" not in successor_env
        assert AUTO_PORT_FILE_ENV not in successor_env
        assert successor_env["TAKKUB_RESTART_SUCCESSOR"] == "1"

    def test_successor_preserves_explicit_shell_port_file_override(
        self,
        qapp: QCoreApplication,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: pathlib.Path,
    ) -> None:
        explicit_port_file = tmp_path / "custom-port"
        monkeypatch.setenv("TAKKUB_PORT_FILE", str(explicit_port_file))
        monkeypatch.delenv(AUTO_PORT_FILE_ENV, raising=False)
        monkeypatch.setattr(up_mod, "QCoreApplication", MagicMock())
        win = self._make_restart_stub(tmp_path)

        popen_kwargs: dict = {}

        def _fake_popen(cmd, **kw):
            popen_kwargs.update(kw)
            return MagicMock()

        with patch("subprocess.Popen", side_effect=_fake_popen):
            mw_mod.MainWindow._restart_cockpit(win)

        assert popen_kwargs["env"]["TAKKUB_PORT_FILE"] == str(explicit_port_file)


class TestMultiInstancePortFileProvenance:
    def test_app_generated_value_is_marked_for_restart(self, tmp_path: pathlib.Path) -> None:
        env: dict[str, str] = {}

        configure_multi_instance_port_file(env, pid=4242, temp_dir=tmp_path)

        expected = str(tmp_path / "agent-takkub-port.4242")
        assert env["TAKKUB_PORT_FILE"] == expected
        assert env[AUTO_PORT_FILE_ENV] == expected

    def test_explicit_shell_value_is_not_marked(self, tmp_path: pathlib.Path) -> None:
        explicit = str(tmp_path / "custom-port")
        env = {"TAKKUB_PORT_FILE": explicit}

        configure_multi_instance_port_file(env, pid=4242, temp_dir=tmp_path)

        assert env["TAKKUB_PORT_FILE"] == explicit
        assert AUTO_PORT_FILE_ENV not in env

    def test_inherited_app_value_is_rederived_for_current_pid(self, tmp_path: pathlib.Path) -> None:
        inherited = str(tmp_path / "agent-takkub-port.111")
        env = {
            "TAKKUB_PORT_FILE": inherited,
            AUTO_PORT_FILE_ENV: inherited,
        }

        configure_multi_instance_port_file(env, pid=222, temp_dir=tmp_path)

        expected = str(tmp_path / "agent-takkub-port.222")
        assert env["TAKKUB_PORT_FILE"] == expected
        assert env[AUTO_PORT_FILE_ENV] == expected

    def test_stale_marker_cannot_relabel_explicit_override(self, tmp_path: pathlib.Path) -> None:
        explicit = str(tmp_path / "custom-port")
        env = {
            "TAKKUB_PORT_FILE": explicit,
            AUTO_PORT_FILE_ENV: str(tmp_path / "agent-takkub-port.111"),
        }

        configure_multi_instance_port_file(env, pid=222, temp_dir=tmp_path)

        assert env["TAKKUB_PORT_FILE"] == explicit
        assert AUTO_PORT_FILE_ENV not in env


# ─────────────────────────────────────────────────────────────
# 6. _release_port_file — uses the EFFECTIVE port file, not the static
#    config.PORT_FILE constant (finding 5, isolation-plan crosscheck)
# ─────────────────────────────────────────────────────────────


class TestReleasePortFile:
    def test_deletes_effective_override_port_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        pf = tmp_path / "custom-port"
        pf.write_text("1234", encoding="utf-8")
        monkeypatch.setenv("TAKKUB_PORT_FILE", str(pf))

        up_mod._release_port_file()

        assert not pf.exists()

    def test_missing_file_is_a_noop(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.setenv("TAKKUB_PORT_FILE", str(tmp_path / "does-not-exist"))
        up_mod._release_port_file()  # must not raise

    def test_falls_back_to_static_port_file_when_no_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        monkeypatch.delenv("TAKKUB_PORT_FILE", raising=False)
        pf = tmp_path / "port"
        pf.write_text("1", encoding="utf-8")
        monkeypatch.setattr(up_mod.config, "PORT_FILE", pf)

        up_mod._release_port_file()

        assert not pf.exists()
