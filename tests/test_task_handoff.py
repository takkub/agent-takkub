"""Tests for the file-based task handoff (issue #1).

Covers the pure helper in orchestrator_text.py (`_task_handoff_pointer` /
`_task_handoff_dir`) and its integration into `Orchestrator._assign_dispatch`:

  1. short composed task (< threshold) pastes directly, no file written
  2. long composed task (>= threshold) writes a handoff file and returns a
     short pointer instead
  3. the handoff file's content is byte-identical to the full task
  4. the pointer always uses forward slashes, even on Windows
  5. a write failure degrades to pasting the full task inline (no crash)
  6. `_assign_dispatch` stores the FULL task in last_assigned_task regardless
     of pointer/inline, and remembers the handoff file path (or None) on
     PaneState
"""

from __future__ import annotations

import pathlib
from unittest.mock import patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import Orchestrator, _exit_key
from agent_takkub.orchestrator_text import (
    TASK_HANDOFF_THRESHOLD,
    _task_handoff_pointer,
)

TEST_PROJECT = "handofftest"


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


class TestTaskHandoffPointer:
    def test_short_task_pastes_directly(self) -> None:
        task = "[ROLE: backend] add a health check endpoint"
        assert len(task) < TASK_HANDOFF_THRESHOLD
        paste_text, task_file = _task_handoff_pointer(task, TEST_PROJECT, "backend")
        assert paste_text == task
        assert task_file is None

    def test_long_task_writes_file_and_returns_pointer(self) -> None:
        task = "[ROLE: backend] " + ("x" * TASK_HANDOFF_THRESHOLD)
        paste_text, task_file = _task_handoff_pointer(task, TEST_PROJECT, "backend")
        assert task_file is not None
        assert paste_text != task
        assert "[ROLE: backend]" in paste_text
        assert "Read tool" in paste_text
        assert "takkub done" in paste_text
        assert task_file in paste_text

    def test_file_content_matches_full_task_verbatim(self) -> None:
        task = "[ROLE: qa] " + ("y" * TASK_HANDOFF_THRESHOLD)
        _paste_text, task_file = _task_handoff_pointer(task, TEST_PROJECT, "qa")
        assert task_file is not None
        assert pathlib.Path(task_file).read_text(encoding="utf-8") == task

    def test_pointer_uses_forward_slashes(self) -> None:
        task = "[ROLE: qa] " + ("z" * TASK_HANDOFF_THRESHOLD)
        paste_text, task_file = _task_handoff_pointer(task, TEST_PROJECT, "qa")
        assert task_file is not None
        assert "\\" not in task_file
        assert "\\" not in paste_text

    def test_write_failure_falls_back_to_inline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        task = "[ROLE: backend] " + ("x" * TASK_HANDOFF_THRESHOLD)

        def _boom(*_a, **_kw):
            raise OSError("disk full")

        monkeypatch.setattr(pathlib.Path, "write_text", _boom)
        paste_text, task_file = _task_handoff_pointer(task, TEST_PROJECT, "backend")
        assert paste_text == task
        assert task_file is None

    def test_shard_role_name_is_filesystem_safe(self) -> None:
        # Shard roles look like "qa#1" — '#' is a legal filename char on both
        # Windows and POSIX, so no extra sanitization should be needed.
        task = "[ROLE: qa#1] " + ("x" * TASK_HANDOFF_THRESHOLD)
        _paste_text, task_file = _task_handoff_pointer(task, TEST_PROJECT, "qa#1")
        assert task_file is not None
        assert pathlib.Path(task_file).exists()


class TestAssignDispatchHandoff:
    def test_short_task_stored_full_and_pasted_full(self, orch: Orchestrator) -> None:
        ekey = _exit_key(TEST_PROJECT, "backend")
        task = "[ROLE: backend] add /health endpoint"

        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready") as mock_send,
        ):
            orch.assign("backend", cwd="/api", task=task, project=TEST_PROJECT)

        ps = orch._pane_state[ekey]
        assert ps.last_assigned_task == task
        assert ps.last_assigned_task_file is None
        assert mock_send.call_args.args[1] == task

    def test_long_task_stored_full_but_pasted_as_pointer(self, orch: Orchestrator) -> None:
        ekey = _exit_key(TEST_PROJECT, "backend")
        task = "[ROLE: backend] " + ("a" * TASK_HANDOFF_THRESHOLD)

        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready") as mock_send,
        ):
            orch.assign("backend", cwd="/api", task=task, project=TEST_PROJECT)

        ps = orch._pane_state[ekey]
        # Full text always in last_assigned_task — this is the crash-replay
        # unit (spawn_engine._auto_respawn) and must never be a pointer.
        assert ps.last_assigned_task == task
        assert ps.last_assigned_task_file is not None
        assert pathlib.Path(ps.last_assigned_task_file).read_text(encoding="utf-8") == task

        pasted = mock_send.call_args.args[1]
        assert pasted != task
        assert ps.last_assigned_task_file in pasted

    def test_fresh_assign_clears_stale_task_file(self, orch: Orchestrator) -> None:
        """A pane's second assign() must not carry over a stale task_file
        pointer from its first (long) assignment when the new task is short."""
        ekey = _exit_key(TEST_PROJECT, "backend")
        long_task = "[ROLE: backend] " + ("a" * TASK_HANDOFF_THRESHOLD)
        short_task = "[ROLE: backend] tiny follow-up"

        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready"),
        ):
            orch.assign("backend", cwd="/api", task=long_task, project=TEST_PROJECT)
            assert orch._pane_state[ekey].last_assigned_task_file is not None
            orch.assign("backend", cwd="/api", task=short_task, project=TEST_PROJECT)

        assert orch._pane_state[ekey].last_assigned_task_file is None
