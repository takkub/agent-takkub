"""Tests for `takkub task show --role <r>` (issue #1 file-based task handoff).

Covers the full round trip:
  1. Orchestrator.task_show_info() — inline (no file) vs. handoff-file cases
  2. cli_server "task-show" dispatch
  3. cli.py cmd_task() output formatting
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import Orchestrator, _exit_key

TEST_PROJECT = "taskshowtest"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or TEST_PROJECT),
    )
    o = Orchestrator()
    o._idle_watchdog.stop()
    return o


class TestTaskShowInfo:
    def test_no_task_assigned_returns_error(self, orch: Orchestrator) -> None:
        ok, msg, payload = orch.task_show_info("backend", project=TEST_PROJECT)
        assert ok is False
        assert "no task assigned" in msg
        assert payload == {}

    def test_inline_task_returns_full_text(self, orch: Orchestrator) -> None:
        ekey = _exit_key(TEST_PROJECT, "backend")
        orch._ps(ekey).last_assigned_task = "[ROLE: backend] short task"
        ok, _msg, payload = orch.task_show_info("backend", project=TEST_PROJECT)
        assert ok is True
        assert payload["task"] == "[ROLE: backend] short task"
        assert payload["task_file"] is None

    def test_handoff_file_task_reads_from_disk(
        self, orch: Orchestrator, tmp_path: pathlib.Path
    ) -> None:
        task_file = tmp_path / "task.md"
        task_file.write_text("[ROLE: backend] very long task body", encoding="utf-8")
        ekey = _exit_key(TEST_PROJECT, "backend")
        orch._ps(ekey).last_assigned_task = "[ROLE: backend] very long task body"
        orch._ps(ekey).last_assigned_task_file = str(task_file)
        ok, _msg, payload = orch.task_show_info("backend", project=TEST_PROJECT)
        assert ok is True
        assert payload["task"] == "[ROLE: backend] very long task body"
        assert payload["task_file"] == str(task_file)

    def test_unreadable_task_file_returns_error(self, orch: Orchestrator) -> None:
        ekey = _exit_key(TEST_PROJECT, "backend")
        orch._ps(ekey).last_assigned_task = "[ROLE: backend] task"
        orch._ps(ekey).last_assigned_task_file = "/nonexistent/path/task.md"
        ok, msg, payload = orch.task_show_info("backend", project=TEST_PROJECT)
        assert ok is False
        assert "unreadable" in msg
        assert payload == {}


class TestCliServerTaskShowDispatch:
    @pytest.fixture
    def srv_and_sock(self, qapp: QCoreApplication):
        from agent_takkub.cli_server import CliServer

        class _FakeSock:
            def __init__(self) -> None:
                self._buf = b""

            def write(self, data: bytes) -> None:
                self._buf += data

            def flush(self) -> None:
                pass

            def last_response(self) -> dict:
                line = self._buf.split(b"\n", 1)[0]
                return json.loads(line.decode("utf-8"))

        mock_orch = MagicMock()
        mock_orch._lead_token = "tok"
        mock_orch.task_show_info.return_value = (
            True,
            "task",
            {"task": "[ROLE: backend] full text", "task_file": None},
        )
        srv = CliServer(mock_orch)
        return srv, _FakeSock(), mock_orch

    def test_task_show_dispatch_returns_payload(self, srv_and_sock) -> None:
        srv, sock, mock_orch = srv_and_sock
        srv._dispatch(sock, {"cmd": "task-show", "role": "backend", "from": "backend"})
        resp = sock.last_response()
        assert resp["ok"] is True
        assert resp["task"] == "[ROLE: backend] full text"
        mock_orch.task_show_info.assert_called_once()

    def test_task_show_dispatch_propagates_error(self, srv_and_sock) -> None:
        srv, sock, mock_orch = srv_and_sock
        mock_orch.task_show_info.return_value = (False, "no task assigned to 'qa' yet", {})
        srv._dispatch(sock, {"cmd": "task-show", "role": "qa", "from": "qa"})
        resp = sock.last_response()
        assert resp["ok"] is False
        assert "no task assigned" in resp["msg"]

    def test_task_show_not_lead_gated(self, srv_and_sock) -> None:
        # Any pane may read back its own task — not restricted to Lead.
        from agent_takkub.cli_server import _LEAD_ONLY_CMDS

        assert "task-show" not in _LEAD_ONLY_CMDS


class TestCmdTask:
    def test_show_prints_task_and_returns_ok(self, capsys) -> None:
        import argparse

        from agent_takkub.cli import cmd_task

        with patch(
            "agent_takkub.cli._request",
            return_value={"ok": True, "task": "[ROLE: backend] full text", "task_file": None},
        ):
            args = argparse.Namespace(t_cmd="show", role="backend")
            result = cmd_task(args)

        assert result["ok"] is True
        out = capsys.readouterr().out
        assert "[ROLE: backend] full text" in out

    def test_show_prints_task_file_path_when_present(self, capsys) -> None:
        import argparse

        from agent_takkub.cli import cmd_task

        with patch(
            "agent_takkub.cli._request",
            return_value={
                "ok": True,
                "task": "full text",
                "task_file": "/runtime/tasks/p/2026-07-09/120000-backend.md",
            },
        ):
            args = argparse.Namespace(t_cmd="show", role="backend")
            cmd_task(args)

        out = capsys.readouterr().out
        assert "120000-backend.md" in out

    def test_show_error_returns_exit_code_1(self) -> None:
        import argparse

        from agent_takkub.cli import cmd_task

        with patch(
            "agent_takkub.cli._request",
            return_value={"ok": False, "msg": "no task assigned to 'qa' yet"},
        ):
            args = argparse.Namespace(t_cmd="show", role="qa")
            result = cmd_task(args)

        assert result["ok"] is False
        assert result["exit_code"] == 1
