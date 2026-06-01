"""Tests for auto-respawn task replay.

When a pane crashes and _auto_respawn() fires, it should re-deliver the last
task that was sent via assign() so the user doesn't end up with an empty pane.
Three cases are verified:
  1. assign → crash → _auto_respawn → task replayed
  2. spawn only (no assign) → crash → _auto_respawn → no replay
  3. assign → manual close → (hypothetical) _auto_respawn → no replay (cache cleared)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import Orchestrator, _exit_key

TEST_PROJECT = "testproj"

SAMPLE_TASK = "[ROLE: backend] implement /auth/logout endpoint\n\ntakkub done when done"


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


def _make_alive_pane() -> MagicMock:
    pane = MagicMock()
    pane.state = "working"
    pane.session = MagicMock()
    pane.session.is_alive = True
    pane.session.is_at_ready_prompt.return_value = True
    return pane


class TestAutoRespawnReplay:
    def test_task_replayed_after_crash(self, orch: Orchestrator) -> None:
        """assign() caches task; _auto_respawn() replays it on fresh spawn."""
        ekey = _exit_key(TEST_PROJECT, "backend")

        # Simulate: assign was called and cached the task
        orch._last_assigned_task[ekey] = SAMPLE_TASK

        # Simulate: pane exists in exited state (crashed)
        crashed_pane = MagicMock()
        crashed_pane.session = None
        crashed_pane.state = "exited"
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = crashed_pane

        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")) as mock_spawn,
            patch.object(orch, "_send_when_ready") as mock_send,
        ):
            orch._auto_respawn("backend", "/some/cwd", TEST_PROJECT)

        mock_spawn.assert_called_once_with(
            "backend", cwd="/some/cwd", project=TEST_PROJECT, _from_auto_respawn=True
        )
        mock_send.assert_called_once_with("backend", SAMPLE_TASK, project=TEST_PROJECT)

    def test_no_replay_when_no_prior_assign(self, orch: Orchestrator) -> None:
        """If user spawned the pane directly (no assign), _auto_respawn doesn't replay."""
        # No entry in _last_assigned_task for this role

        crashed_pane = MagicMock()
        crashed_pane.session = None
        crashed_pane.state = "exited"
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["devops"] = crashed_pane

        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready") as mock_send,
        ):
            orch._auto_respawn("devops", "/some/cwd", TEST_PROJECT)

        mock_send.assert_not_called()

    def test_no_replay_after_manual_close(self, orch: Orchestrator) -> None:
        """close() clears the cache so a manual restart doesn't re-inject stale task."""
        ekey = _exit_key(TEST_PROJECT, "qa")
        orch._last_assigned_task[ekey] = SAMPLE_TASK

        pane = _make_alive_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["qa"] = pane
        pane.mark_expected_exit = MagicMock()
        pane.session.terminate = MagicMock()
        pane.set_state = MagicMock()

        # close() should clear the cache
        orch.close("qa", project=TEST_PROJECT)

        assert ekey not in orch._last_assigned_task

    def test_no_replay_when_spawn_fails(self, orch: Orchestrator) -> None:
        """If spawn fails, _auto_respawn must not attempt _send_when_ready."""
        ekey = _exit_key(TEST_PROJECT, "mobile")
        orch._last_assigned_task[ekey] = SAMPLE_TASK

        crashed_pane = MagicMock()
        crashed_pane.session = None
        crashed_pane.state = "exited"
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["mobile"] = crashed_pane

        with (
            patch.object(orch, "spawn", return_value=(False, "spawn failed")),
            patch.object(orch, "_send_when_ready") as mock_send,
        ):
            orch._auto_respawn("mobile", "/some/cwd", TEST_PROJECT)

        mock_send.assert_not_called()

    def test_assign_stores_task_in_cache(self, orch: Orchestrator) -> None:
        """assign() must persist the task before calling _send_when_ready."""
        ekey = _exit_key(TEST_PROJECT, "frontend")

        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready"),
        ):
            orch.assign("frontend", cwd="/web", task=SAMPLE_TASK, project=TEST_PROJECT)

        assert orch._last_assigned_task.get(ekey) == SAMPLE_TASK

    def test_assign_rewrites_codex_task_with_override_notice(self, orch: Orchestrator) -> None:
        """assign() must prepend the override notice when the role is backed
        by codex — otherwise codex over-reads `ห้าม spawn subagent` and skips
        `takkub done` as a shell command (regression guard for 9fd6001).
        Asserts the call-site, not just the rewriter helper, so removing
        the assign() integration breaks a test.
        """
        from agent_takkub.orchestrator import _CODEX_TASK_NOTICE

        ekey = _exit_key(TEST_PROJECT, "codex")
        raw_task = (
            "[ROLE: codex reviewer — ทำงานเองโดยตรง ห้าม spawn subagent]\nCross-check refactor X."
        )

        # The rewrite gate uses effective_provider_for(), which degrades codex
        # → claude when the codex CLI isn't installed (provider substitution).
        # CI runners have no codex binary, so force it "available" here to test
        # the rewrite deterministically regardless of environment.
        with (
            patch("agent_takkub.provider_config._provider_available", return_value=True),
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready") as mock_send,
        ):
            orch.assign("codex", cwd="/web", task=raw_task, project=TEST_PROJECT)

        cached = orch._last_assigned_task[ekey]
        assert cached.startswith(_CODEX_TASK_NOTICE)
        sent_task = mock_send.call_args.args[1]
        assert sent_task.startswith(_CODEX_TASK_NOTICE)

    def test_assign_does_not_rewrite_non_codex_task(self, orch: Orchestrator) -> None:
        """Non-codex roles must NOT receive the codex-specific override
        notice — claude/gemini panes have their own rules and the notice
        text references codex-only context."""
        from agent_takkub.orchestrator import _CODEX_TASK_NOTICE

        ekey = _exit_key(TEST_PROJECT, "backend")
        raw_task = "[ROLE: backend] implement /auth/logout"

        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready"),
        ):
            orch.assign("backend", cwd="/api", task=raw_task, project=TEST_PROJECT)

        cached = orch._last_assigned_task[ekey]
        assert _CODEX_TASK_NOTICE not in cached
        assert cached == raw_task

    def test_done_clears_replay_cache_so_late_crash_does_not_replay(
        self, orch: Orchestrator
    ) -> None:
        """done() must pop _last_assigned_task so a crash within the 2.5 s close-
        window doesn't replay an already-completed task on auto-respawn."""
        ekey = _exit_key(TEST_PROJECT, "reviewer")
        orch._last_assigned_task[ekey] = SAMPLE_TASK

        pane = _make_alive_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["reviewer"] = pane

        with patch.object(orch, "_send_when_ready"):
            orch.done("reviewer", note="done", project=TEST_PROJECT)

        # Cache must be cleared immediately (before the 2.5 s close fires)
        assert ekey not in orch._last_assigned_task

        # Simulate session exit within the 2.5 s window → _auto_respawn fires
        crashed_pane = MagicMock()
        crashed_pane.session = None
        crashed_pane.state = "exited"
        orch._panes_by_project[TEST_PROJECT]["reviewer"] = crashed_pane

        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")),
            patch.object(orch, "_send_when_ready") as mock_send_after,
        ):
            orch._auto_respawn("reviewer", "/some/cwd", TEST_PROJECT)

        mock_send_after.assert_not_called()
