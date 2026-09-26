"""Tests for the opt-in --requires-commit done handoff signal.

The handoff runs inside orchestrator.done() when assign() was called with
requires_commit=True. M2: the dirty-tree check now runs ASYNCHRONOUSLY via
QProcess so a slow/large repo can't freeze the Qt main thread. done() returns
immediately and, if the working tree turns out dirty, a follow-up
`[requires-commit]` warning is delivered to Lead.

Split of concerns now:
  - `_uncommitted_warning(role, porcelain)` — pure: dirty → warning string
    (with files preview), clean/blank → None.
  - `_check_uncommitted_async(project, role, cwd)` — fires the git QProcess and
    delivers the warning; done() calls it iff the flag is set.
  - done() itself no longer blocks on git and no longer inlines the warning.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import Orchestrator, PaneState, _exit_key

TEST_PROJECT = "testproj"


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


def _make_working_pane(cwd: str = "/repo") -> MagicMock:
    pane = MagicMock()
    pane.state = "working"
    pane.session = MagicMock()
    pane.session.is_alive = True
    pane.session.is_at_ready_prompt.return_value = True
    pane._session_cwd = cwd
    pane._transcript_path = None
    return pane


def _make_lead_pane() -> MagicMock:
    pane = MagicMock()
    pane.session = MagicMock()
    pane.session.is_alive = True
    return pane


def _written_str(mock_session: MagicMock) -> str:
    parts: list[str] = []
    for c in mock_session.write.call_args_list:
        arg = c.args[0] if c.args else ""
        parts.append(arg.decode("utf-8", "replace") if isinstance(arg, bytes) else str(arg))
    return "".join(parts)


# ──────────────────────────────────────────────────────────────────────
# Pure: _uncommitted_warning
# ──────────────────────────────────────────────────────────────────────


def test_codex_queued_followup_blocks_done_until_queue_runs(orch, monkeypatch, tmp_path):
    pane = _make_working_pane(str(tmp_path))
    pane.model.provider_name = "codex"
    pane.session.shows_busy_queue_confirm.return_value = True
    pane.set_state.side_effect = lambda state, **kw: setattr(pane, "state", state)
    orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane
    ps = orch._ps(_exit_key(TEST_PROJECT, "backend"))
    ps.last_assigned_task = "backup"
    ps.task_delivered = True
    callbacks = []
    monkeypatch.setattr(
        "agent_takkub.orchestrator.QTimer.singleShot", lambda ms, cb: callbacks.append((ms, cb))
    )
    with patch.object(orch, "_notify_lead") as notify:
        ok, msg = orch.done("backend", note="backup finished", project=TEST_PROJECT)
        assert not ok and "queued follow-up" in msg
        assert pane.state == "working"
        assert not callbacks
        ok, _ = orch.done("backend", note="backup finished", project=TEST_PROJECT)
        assert not ok
        assert notify.call_count == 1

        pane.session.shows_busy_queue_confirm.return_value = False
        ok, msg = orch.done("backend", note="follow-up finished", project=TEST_PROJECT)
        assert ok, msg
        assert pane.state == "done"


@pytest.mark.parametrize("provider", ["claude", "codex", "gemini", "opencode", "kimi", "cursor"])
def test_followup_survives_done_and_stale_close(orch, monkeypatch, tmp_path, provider):
    # This case exercises the kept-pane queue handoff. The default close-on-done
    # path has its own coverage and intentionally replaces a finished pane.
    monkeypatch.setattr("agent_takkub.orchestrator.CLOSE_ON_DONE", False)
    key = _exit_key(TEST_PROJECT, "backend")
    pane = _make_working_pane(str(tmp_path))
    pane.set_state.side_effect = lambda state, **kw: setattr(pane, "state", state)
    orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane
    old = orch._ps(key)
    old.last_assigned_task = "first task"
    old.task_id = "first-id"
    old.task_delivered = True
    old.provider_override = provider
    callbacks = []
    monkeypatch.setattr(
        "agent_takkub.orchestrator.QTimer.singleShot", lambda ms, cb: callbacks.append((ms, cb))
    )
    with (
        patch.object(orch, "spawn", return_value=(True, "running")),
        patch.object(orch, "_send_when_ready") as send,
    ):
        assert orch._assign_dispatch("backend", str(tmp_path), "second task", project=TEST_PROJECT)[
            0
        ]
        assert old.task_id == "first-id"
        assert old.last_assigned_task == "first task"
        assert orch.done("backend", note="first finished", project=TEST_PROJECT)[0]
        assert not any(ms == 2500 for ms, cb in callbacks)
        for ms, cb in list(callbacks):
            if ms == 0:
                cb()
        assert "second task" in orch._ps(key).last_assigned_task
        assert orch._ps(key).task_id != "first-id"
        assert len(send.call_args_list) == 1
        assert "second task" in send.call_args.args[1]


def test_close_drops_pending_assignment_by_default(orch, monkeypatch, tmp_path):
    # #593 H1: a plain close() (CLI `takkub close` / user pane-x click, no
    # keep_queue) must NOT bring the role back to run a queued next task —
    # that would silently override an explicit "stop" with a respawn.
    key = _exit_key(TEST_PROJECT, "backend")
    pane = _make_working_pane(str(tmp_path))
    orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane
    ps = orch._ps(key)
    ps.last_assigned_task = "active"
    ps.task_delivered = True
    callbacks = []
    monkeypatch.setattr(
        "agent_takkub.orchestrator.QTimer.singleShot", lambda ms, cb: callbacks.append(cb)
    )
    orch._assign_dispatch("backend", str(tmp_path), "pending", project=TEST_PROJECT)
    orch.paneClosed.connect(lambda role, project: orch._panes_by_project[project].pop(role, None))
    with (
        patch.object(orch, "_warn_if_live_children"),
        patch.object(orch, "_notify_lead") as notify,
        patch.object(orch, "_dispatch_next_assignment", return_value=True) as dispatch,
    ):
        assert orch.close("backend", project=TEST_PROJECT)[0]
        for cb in list(callbacks):
            cb()
        dispatch.assert_not_called()
        assert not orch._pending_assignments.get(key)
        # text stays recoverable via `takkub task show --role backend`, same
        # preservation shape as #484's undelivered-task keep.
        assert orch._pane_state[key].last_assigned_task == "pending"
        assert orch._pane_state[key].task_delivered is False
        assert any(
            call.kwargs.get("kind") == "close-dropped-queue" for call in notify.call_args_list
        )


def test_close_keep_queue_forwards_pending_assignment(orch, monkeypatch, tmp_path):
    # System-internal replace closes (#603 idle-provider-switch, #514 quota
    # reroute, the stuck/no-content/auth watchdogs) pass keep_queue=True and
    # must keep forwarding the queue exactly as before.
    key = _exit_key(TEST_PROJECT, "backend")
    pane = _make_working_pane(str(tmp_path))
    orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane
    ps = orch._ps(key)
    ps.last_assigned_task = "active"
    ps.task_delivered = True
    callbacks = []
    monkeypatch.setattr(
        "agent_takkub.orchestrator.QTimer.singleShot", lambda ms, cb: callbacks.append(cb)
    )
    orch._assign_dispatch("backend", str(tmp_path), "pending", project=TEST_PROJECT)
    orch.paneClosed.connect(lambda role, project: orch._panes_by_project[project].pop(role, None))
    with (
        patch.object(orch, "_warn_if_live_children"),
        patch.object(orch, "_notify_lead"),
        patch.object(orch, "_dispatch_next_assignment", return_value=True) as dispatch,
    ):
        assert orch.close("backend", project=TEST_PROJECT, keep_queue=True)[0]
        for cb in list(callbacks):
            cb()
        dispatch.assert_called_once_with(TEST_PROJECT, "backend")
        assert orch._pending_assignments[key][0]["task"] == "pending"


def test_busy_requeue_preserves_fifo_order(orch, tmp_path):
    # M3: `_dispatch_next_assignment` pops the queue's FRONT item and
    # re-delivers it via `_assign_dispatch`; if the pane is still busy at
    # that instant, the busy-check re-queues it — it must go back to
    # position 0, not the end, or [A, B, C] silently becomes [B, C, A].
    key = _exit_key(TEST_PROJECT, "backend")
    pane = _make_working_pane(str(tmp_path))
    orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane
    ps = orch._ps(key)
    ps.last_assigned_task = "active"
    ps.task_delivered = True
    for label in ("A", "B", "C"):
        orch._assign_dispatch("backend", str(tmp_path), label, project=TEST_PROJECT)
    assert [item["task"] for item in orch._pending_assignments[key]] == ["A", "B", "C"]

    with patch.object(orch, "_notify_lead"):
        orch._dispatch_next_assignment(TEST_PROJECT, "backend")

    assert [item["task"] for item in orch._pending_assignments[key]] == ["A", "B", "C"]


class TestUncommittedWarning:
    def test_dirty_returns_warning_with_preview(self) -> None:
        out = Orchestrator._uncommitted_warning("frontend", "M src/app.tsx\nA test.tsx\n")
        assert out is not None
        assert "requires-commit" in out
        assert "uncommitted" in out
        assert "frontend" in out
        assert "src/app.tsx" in out

    def test_clean_returns_none(self) -> None:
        assert Orchestrator._uncommitted_warning("qa", "") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert Orchestrator._uncommitted_warning("qa", "   \n  \n") is None

    def test_preview_capped_at_200(self) -> None:
        big = "M " + ("x" * 5000) + "\n"
        out = Orchestrator._uncommitted_warning("qa", big)
        assert out is not None
        # only the 200-char preview of the porcelain output is embedded ("M " + 198 x)
        assert "x" * 190 in out
        assert "x" * 300 not in out


# ──────────────────────────────────────────────────────────────────────
# done() wires the async check by flag
# ──────────────────────────────────────────────────────────────────────


class TestDoneGateAsync:
    def _assign_with_flag(self, orch: Orchestrator, role: str, flag: bool) -> None:
        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready"),
        ):
            orch.assign(
                role, cwd="/repo", task="do work", requires_commit=flag, project=TEST_PROJECT
            )

    def test_no_flag_skips_async_check(self, orch: Orchestrator) -> None:
        pane = _make_working_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane
        orch._panes_by_project[TEST_PROJECT]["lead"] = _make_lead_pane()
        self._assign_with_flag(orch, "backend", flag=False)

        with patch.object(orch, "_check_uncommitted_async") as chk:
            ok, _ = orch.done("backend", note="done", project=TEST_PROJECT)
        assert ok is True
        chk.assert_not_called()

    def test_flag_fires_async_check_with_cwd(self, orch: Orchestrator) -> None:
        pane = _make_working_pane(cwd="/repo")
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["frontend"] = pane
        orch._panes_by_project[TEST_PROJECT]["lead"] = _make_lead_pane()
        self._assign_with_flag(orch, "frontend", flag=True)

        with patch.object(orch, "_check_uncommitted_async") as chk:
            ok, msg = orch.done("frontend", note="done", project=TEST_PROJECT)
        assert ok is True
        assert "rejected" not in msg
        chk.assert_called_once_with(TEST_PROJECT, "frontend", "/repo")

    def test_main_notice_has_no_inline_warning(self, orch: Orchestrator) -> None:
        # The immediate done notice never carries the warning now — it arrives as
        # a follow-up from the async check.
        pane = _make_working_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["frontend"] = pane
        lead = _make_lead_pane()
        orch._panes_by_project[TEST_PROJECT]["lead"] = lead
        self._assign_with_flag(orch, "frontend", flag=True)

        with patch.object(orch, "_check_uncommitted_async"):
            orch.done("frontend", note="done", project=TEST_PROJECT)
        assert "requires-commit" not in _written_str(lead.session)

    def test_async_check_only_for_flagged_pane(self, orch: Orchestrator) -> None:
        reviewer = _make_working_pane()
        designer = _make_working_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["reviewer"] = reviewer
        orch._panes_by_project[TEST_PROJECT]["designer"] = designer
        orch._panes_by_project[TEST_PROJECT]["lead"] = _make_lead_pane()
        orch._ps(_exit_key(TEST_PROJECT, "reviewer")).requires_commit_on_done = True

        with patch.object(orch, "_check_uncommitted_async") as chk:
            orch.done("designer", note="done", project=TEST_PROJECT)
            chk.assert_not_called()
            orch.done("reviewer", note="done", project=TEST_PROJECT)
            chk.assert_called_once()
            assert chk.call_args.args[1] == "reviewer"


# ──────────────────────────────────────────────────────────────────────
# Flag lifecycle (independent of the git check)
# ──────────────────────────────────────────────────────────────────────


class TestRequiresCommitFlagLifecycle:
    def test_done_clears_flag(self, orch: Orchestrator) -> None:
        pane = _make_working_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["devops"] = pane
        ekey = _exit_key(TEST_PROJECT, "devops")
        orch._ps(ekey).requires_commit_on_done = True

        with patch.object(orch, "_check_uncommitted_async"):
            ok, _ = orch.done("devops", note="done", project=TEST_PROJECT)
        assert ok is True
        assert not (orch._pane_state.get(ekey) or PaneState()).requires_commit_on_done

    def test_close_clears_requires_commit_flag(self, orch: Orchestrator) -> None:
        pane = _make_working_pane()
        pane.mark_expected_exit = MagicMock()
        pane.session.terminate = MagicMock()
        pane.set_state = MagicMock()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["qa"] = pane
        ekey = _exit_key(TEST_PROJECT, "qa")
        orch._ps(ekey).requires_commit_on_done = True

        orch.close("qa", project=TEST_PROJECT)
        assert orch._pane_state.get(ekey) is None
