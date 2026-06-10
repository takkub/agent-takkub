"""Tests for the session-goal feature (issue #50).

`takkub goal "<objective>"` stores a per-project objective in the orchestrator
(volatile, never persisted). Every subsequent `assign` task is prepended with a
context block so parallel teammates share the big picture and don't drift on
scope. `takkub goal --clear` unsets it; `takkub goal` (no arg) shows it.

Covered here (logic layer — no panes needed):
  1. set → get round-trips
  2. _apply_session_goal prepends the header + goal before the task
  3. no goal set → _apply_session_goal is a no-op
  4. idempotent: applying twice does not double-prepend (respawn-replay guard)
  5. clear → get returns None and apply is a no-op again
  6. empty goal text is rejected
  7. goals are isolated per project (no cross-tab leak)
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import _SESSION_GOAL_HEADER, Orchestrator

TEST_PROJECT = "testproj"
OTHER_PROJECT = "otherproj"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    # Identity project resolution so a None project maps to TEST_PROJECT and
    # explicit project names pass through unchanged (for the isolation test).
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or TEST_PROJECT),
    )
    o = Orchestrator()
    o._idle_watchdog.stop()
    return o


def test_set_then_get_roundtrips(orch: Orchestrator) -> None:
    ok, _ = orch.set_session_goal("ship RBAC v1", project=TEST_PROJECT)
    assert ok
    assert orch.get_session_goal(project=TEST_PROJECT) == "ship RBAC v1"


def test_apply_prepends_header_and_goal(orch: Orchestrator) -> None:
    orch.set_session_goal("ship RBAC v1", project=TEST_PROJECT)
    out = orch._apply_session_goal("[ROLE: backend] add POST /roles", TEST_PROJECT)
    assert out.startswith(_SESSION_GOAL_HEADER)
    assert "ship RBAC v1" in out
    assert out.endswith("[ROLE: backend] add POST /roles")


def test_apply_noop_when_unset(orch: Orchestrator) -> None:
    task = "[ROLE: frontend] role selector"
    assert orch._apply_session_goal(task, TEST_PROJECT) == task


def test_apply_is_idempotent(orch: Orchestrator) -> None:
    orch.set_session_goal("ship RBAC v1", project=TEST_PROJECT)
    once = orch._apply_session_goal("do the thing", TEST_PROJECT)
    twice = orch._apply_session_goal(once, TEST_PROJECT)
    # Re-applying the already-prepended task must not stack a second header
    # (auto-respawn replays the stored last_assigned_task, which already has it).
    assert twice == once
    assert once.count(_SESSION_GOAL_HEADER) == 1


def test_clear_unsets(orch: Orchestrator) -> None:
    orch.set_session_goal("ship RBAC v1", project=TEST_PROJECT)
    ok, _ = orch.clear_session_goal(project=TEST_PROJECT)
    assert ok
    assert orch.get_session_goal(project=TEST_PROJECT) is None
    task = "plain task"
    assert orch._apply_session_goal(task, TEST_PROJECT) == task


def test_empty_goal_rejected(orch: Orchestrator) -> None:
    ok, msg = orch.set_session_goal("   ", project=TEST_PROJECT)
    assert not ok
    assert "empty" in msg.lower()
    assert orch.get_session_goal(project=TEST_PROJECT) is None


def test_goals_isolated_per_project(orch: Orchestrator) -> None:
    orch.set_session_goal("goal-A", project=TEST_PROJECT)
    orch.set_session_goal("goal-B", project=OTHER_PROJECT)
    assert orch.get_session_goal(project=TEST_PROJECT) == "goal-A"
    assert orch.get_session_goal(project=OTHER_PROJECT) == "goal-B"
    orch.clear_session_goal(project=TEST_PROJECT)
    # Clearing one project must not touch the other.
    assert orch.get_session_goal(project=OTHER_PROJECT) == "goal-B"
