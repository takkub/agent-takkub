"""Tests for the file-based task handoff (issue #1).

Covers the pure helper in orchestrator_text.py (`_task_handoff_pointer` /
`_task_handoff_dir`) and its integration into `Orchestrator._assign_dispatch`:

  1. the pure helper allows short messages through; production assignments
     force a Markdown file even for short tasks
  2. long composed task (>= threshold) writes a handoff file and returns a
     short pointer instead
  3. the handoff file's content is byte-identical to the full task
  4. the pointer always uses forward slashes, even on Windows
  5. write failures are reported without delivering a full inline fallback
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
    _message_handoff_pointer,
    _task_handoff_pointer,
)
from tests import extract_task_body

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
    o.shutdown_timers()
    return o


class TestTaskHandoffPointer:
    def test_peer_report_is_saved_intact_and_only_pointer_is_delivered(self):
        report = "CHANGED: fixed the login flow\n" + "Evidence details\n" * 100
        pointer = _message_handoff_pointer(report, TEST_PROJECT, "lead")
        assert report not in pointer
        path = pointer.split('"')[1]
        assert pathlib.Path(path).read_text(encoding="utf-8") == report
        assert len(pointer) < len(report)
        assert "takkub done" not in pointer

    def test_peer_report_write_failure_does_not_paste_inline(self, monkeypatch):
        monkeypatch.setattr(
            "agent_takkub.orchestrator_text._task_handoff_pointer",
            lambda *a, **kw: (a[0], None),
        )
        with pytest.raises(OSError, match="message was not sent"):
            _message_handoff_pointer("full report " * 100, TEST_PROJECT, "lead")

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
        assert "file-read tool" in paste_text
        assert "ห้ามรันไฟล์เป็นโปรแกรม" in paste_text
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

        # #587 A5: the handoff file is now opened exclusively (`"x"` mode, via
        # `pathlib.Path.open`) rather than `write_text`, so a collision never
        # silently overwrites an earlier assign's file — patch the primitive
        # `_task_handoff_pointer` actually calls now.
        monkeypatch.setattr(pathlib.Path, "open", _boom)
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

    def test_supports_file_read_false_always_pastes_inline(self) -> None:
        # Legacy adapters can explicitly opt out when they lack filesystem
        # access. Production Codex uses its shell reader instead.
        task = "[ROLE: frontend] " + ("x" * TASK_HANDOFF_THRESHOLD * 3)
        paste_text, task_file = _task_handoff_pointer(
            task, TEST_PROJECT, "frontend", supports_file_read=False
        )
        assert paste_text == task
        assert task_file is None

    def test_supports_file_read_true_is_the_default(self) -> None:
        # Omitting the kwarg must behave exactly as before #273.
        task = "[ROLE: backend] " + ("x" * TASK_HANDOFF_THRESHOLD)
        with_default = _task_handoff_pointer(task, TEST_PROJECT, "backend")
        with_explicit_true = _task_handoff_pointer(
            task, TEST_PROJECT, "backend", supports_file_read=True
        )
        assert with_default[0] != task  # both took the pointer path
        assert with_explicit_true[0] != task


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
        # #585: the task now carries a budget block prefix — compare the body.
        assert extract_task_body(ps.last_assigned_task) == task
        assert ps.last_assigned_task_file is not None
        assert ps.last_assigned_task_file in mock_send.call_args.args[1]
        assert task not in mock_send.call_args.args[1]

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
        assert extract_task_body(ps.last_assigned_task) == task
        assert ps.last_assigned_task_file is not None
        assert (
            extract_task_body(pathlib.Path(ps.last_assigned_task_file).read_text(encoding="utf-8"))
            == task
        )

        pasted = mock_send.call_args.args[1]
        assert pasted != task
        assert ps.last_assigned_task_file in pasted

    def test_codex_role_reads_long_task_from_markdown(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Codex can read Markdown with a shell command even though it has no
        # separate structured file-read tool. Never paste the full task.
        from agent_takkub.provider_config import CODEX

        monkeypatch.setattr(
            "agent_takkub.provider_config.effective_provider_for", lambda *_a, **_kw: CODEX
        )
        ekey = _exit_key(TEST_PROJECT, "frontend")
        task = "[ROLE: frontend] " + ("a" * TASK_HANDOFF_THRESHOLD * 3)

        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready") as mock_send,
        ):
            orch.assign("frontend", cwd="/web", task=task, project=TEST_PROJECT)

        ps = orch._pane_state[ekey]
        # codex's effective provider also rewrites/prepends notice text
        # (_rewrite_task_for_codex) — the ORIGINAL task text must still be
        # in there somewhere, just not pointer-ized.
        assert task in ps.last_assigned_task
        assert ps.last_assigned_task_file is not None
        pasted = mock_send.call_args.args[1]
        assert task not in pasted
        assert ps.last_assigned_task_file in pasted
        assert "คำสั่งอ่านไฟล์ได้" in pasted

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
            old_file = orch._pane_state[ekey].last_assigned_task_file
            orch.assign("backend", cwd="/api", task=short_task, project=TEST_PROJECT)

        new_file = orch._pane_state[ekey].last_assigned_task_file
        assert new_file and new_file != old_file
        assert short_task in pathlib.Path(new_file).read_text(encoding="utf-8")
