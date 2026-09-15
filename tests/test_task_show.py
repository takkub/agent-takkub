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
    o.shutdown_timers()
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


class TestCloseUndeliveredTaskKeepsLedgerRow:
    """#484 point (c): closing a pane whose current task was accepted but
    never actually delivered (e.g. parked on a trust modal the whole time)
    must not wipe `task_show_info` — the text must stay recoverable. A pane
    that DID receive its task before being closed without `done()` keeps the
    prior behaviour (state cleared, ledger row flipped to closed/abandoned)."""

    def _pane(self) -> MagicMock:
        pane = MagicMock()
        pane.session = None  # not alive — close() skips the terminate() branch
        return pane

    def test_close_keeps_task_recoverable_when_never_delivered(self, orch: Orchestrator) -> None:
        orch._panes_by_project[TEST_PROJECT] = {"backend": self._pane()}
        ekey = _exit_key(TEST_PROJECT, "backend")
        orch._ps(ekey).last_assigned_task = "[ROLE: backend] never reached the pane"
        # task_delivered defaults to False — this task was accepted but the
        # pane never got past e.g. a trust modal before being closed.

        with (
            patch("agent_takkub.task_ledger.mark_done", return_value=None),
            patch.object(orch, "_drain_pane_health", return_value=None),
            patch.object(orch, "_notify_lead") as notify,
        ):
            ok, msg = orch.close("backend", project=TEST_PROJECT)

        assert ok is True, msg
        show_ok, _show_msg, payload = orch.task_show_info("backend", project=TEST_PROJECT)
        assert show_ok is True
        assert payload["task"] == "[ROLE: backend] never reached the pane"
        assert any(call.kwargs.get("kind") == "close-undelivered" for call in notify.call_args_list)

    def test_close_clears_state_normally_once_task_was_delivered(self, orch: Orchestrator) -> None:
        orch._panes_by_project[TEST_PROJECT] = {"backend": self._pane()}
        ekey = _exit_key(TEST_PROJECT, "backend")
        orch._ps(ekey).last_assigned_task = "[ROLE: backend] this one landed"
        orch._ps(ekey).task_delivered = True

        with (
            patch("agent_takkub.task_ledger.mark_done", return_value=None),
            patch.object(orch, "_drain_pane_health", return_value=None),
            patch.object(orch, "_notify_lead") as notify,
        ):
            ok, msg = orch.close("backend", project=TEST_PROJECT)

        assert ok is True, msg
        show_ok, show_msg, _payload = orch.task_show_info("backend", project=TEST_PROJECT)
        assert show_ok is False
        assert "no task assigned" in show_msg
        assert not any(
            call.kwargs.get("kind") == "close-undelivered" for call in notify.call_args_list
        )


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
        yield srv, _FakeSock(), mock_orch
        # #345: CliServer.__init__ starts _reaper/_spawn_health unconditionally
        # — stop them (and any pending spawn-stagger timer) so they don't
        # outlive this test as a leaked, still-active QTimer.
        srv.shutdown_timers()

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


@pytest.mark.parametrize(
    "provider", ["claude", "codex", "gemini-agy", "opencode", "kimi", "cursor"]
)
@pytest.mark.parametrize("delivered", [False, True])
def test_recovery_close_does_not_report_abandoned_task_or_finalize_worktree(
    orch: Orchestrator, provider: str, delivered: bool
) -> None:
    pane = MagicMock()
    pane.session = None
    pane.model.provider_name = provider
    orch._panes_by_project[TEST_PROJECT] = {"backend": pane}
    ps = orch._ps(_exit_key(TEST_PROJECT, "backend"))
    ps.last_assigned_task = "continue this task"
    ps.task_delivered = delivered
    ps.worktree = MagicMock()
    with (
        patch("agent_takkub.task_ledger.mark_done", return_value=None),
        patch.object(orch, "_drain_pane_health", return_value=None),
        patch.object(orch, "_notify_lead") as notify,
        patch.object(orch, "_finalize_worktree") as finalize,
        patch.object(orch, "_snapshot_dirty_worktree_if_needed") as snapshot,
    ):
        ok, message = orch.close(
            "backend",
            project=TEST_PROJECT,
            suppress_pipeline=True,
            suppress_auto_chain=True,
            keep_queue=True,
        )
    assert ok, message
    assert not any(c.kwargs.get("kind") == "close-undelivered" for c in notify.call_args_list)
    finalize.assert_not_called()
    snapshot.assert_not_called()
    if not delivered:
        assert (
            orch.task_show_info("backend", project=TEST_PROJECT)[2]["task"] == "continue this task"
        )


@pytest.mark.parametrize(
    "provider", ["claude", "codex", "gemini-agy", "opencode", "kimi", "cursor"]
)
def test_auth_handoff_uses_real_close_and_delivery_in_same_worktree(
    orch: Orchestrator, provider: str, tmp_path: pathlib.Path
) -> None:
    from agent_takkub import orchestrator as orch_mod
    from agent_takkub.worktree_manager import WorktreeInfo

    pane = MagicMock()
    pane.session = None
    pane._session_cwd = str(tmp_path)
    pane._session_generation = 1
    pane.model.provider_name = provider
    lead = MagicMock()
    lead.session.is_alive = True
    orch._panes_by_project[TEST_PROJECT] = {"backend": pane, "lead": lead}
    ps = orch._ps(_exit_key(TEST_PROJECT, "backend"))
    ps.last_assigned_task = "continue original task"
    info = WorktreeInfo(str(tmp_path), "wt/backend-test", "base", str(tmp_path))
    ps.worktree = info
    marker = tmp_path / "existing-work.txt"
    marker.write_text("preserve work", encoding="utf-8")
    spawned = []
    deliveries = []

    def spawn(role, **kwargs):
        spawned.append(kwargs)
        replacement = MagicMock()
        replacement._session_cwd = kwargs["cwd"]
        replacement._session_generation = 2
        replacement.model.provider_name = "claude"
        replacement.session.is_alive = True
        replacement.session.is_at_ready_prompt.return_value = True
        replacement.session.is_at_trust_prompt.return_value = False
        replacement.session.is_blocked_on_tty_prompt.return_value = None
        replacement.session.is_blocked_on_permission_prompt.return_value = None
        replacement.session.first_content_ts.return_value = 1.0
        orch._panes_by_project[TEST_PROJECT][role] = replacement
        return True, "ok"

    def verify(pane, session, *args, **kwargs):
        assert not [
            c for c in notify.call_args_list if c.kwargs.get("kind") == "auth-failure-degrade"
        ]
        session.is_at_ready_prompt.return_value = False
        kwargs["on_settled"]()
        deliveries.extend(session.write.call_args_list)

    with (
        patch.object(orch_mod.QTimer, "singleShot", side_effect=lambda ms, fn: fn()),
        patch.object(orch_mod, "_delayed_enter_verified", side_effect=verify),
        patch.object(orch, "spawn", side_effect=spawn),
        patch.object(orch, "_notify_lead") as notify,
        patch("agent_takkub.task_ledger.mark_done", return_value=None),
    ):
        orch._recover_auth_failed_pane(
            "backend",
            TEST_PROJECT,
            pane,
            ps.last_assigned_task,
            provider=provider,
            reason="not signed in",
        )
    assert spawned[0]["cwd"] == info.path
    assert orch._ps(_exit_key(TEST_PROJECT, "backend")).worktree is info
    assert marker.read_text(encoding="utf-8") == "preserve work"
    assert any("continue original task" in str(c) for c in deliveries)
    assert [c.kwargs.get("kind") for c in notify.call_args_list] == ["auth-failure-degrade"]
