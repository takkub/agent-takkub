"""Tests for Orchestrator.consume_session_report — the SessionStart hook
signal consumer. Fixes session_uuid drift: PaneState.session_uuid used to be
stamped ONLY at spawn time, so a manual `/resume` inside a pane (which
switches claude to a different transcript uuid) left the orchestrator with a
stale uuid and broke the remote mirror's exact-uuid lookup.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import Orchestrator

TEST_PROJECT = "sessiontest"


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


def _key(role: str) -> str:
    return f"{TEST_PROJECT}::{role}"


class TestUpdatesPaneState:
    def test_stamps_new_session_uuid(self, orch: Orchestrator) -> None:
        ok, _msg = orch.consume_session_report(
            "backend",
            project=TEST_PROJECT,
            session_id="new-uuid-after-resume",
            source="resume",
            cwd="/proj",
        )

        assert ok is True
        ps = orch._pane_state[_key("backend")]
        assert ps.session_uuid == "new-uuid-after-resume"
        assert ps.session_uuid_cwd == "/proj"

    def test_overwrites_stale_spawn_time_uuid(self, orch: Orchestrator) -> None:
        # Simulate the spawn-time stamp, then a manual /resume switching claude
        # to a different transcript uuid — the bug this feature fixes.
        ps = orch._ps(_key("backend"))
        ps.session_uuid = "spawn-time-uuid"
        ps.session_uuid_cwd = "/proj"

        ok, _ = orch.consume_session_report(
            "backend",
            project=TEST_PROJECT,
            session_id="resumed-different-uuid",
            source="resume",
            cwd="/proj",
        )

        assert ok is True
        assert orch._pane_state[_key("backend")].session_uuid == "resumed-different-uuid"

    def test_startup_with_same_uuid_is_idempotent_noop(self, orch: Orchestrator) -> None:
        ok1, _ = orch.consume_session_report(
            "backend", project=TEST_PROJECT, session_id="uuid-1", source="startup", cwd="/proj"
        )
        ok2, _ = orch.consume_session_report(
            "backend", project=TEST_PROJECT, session_id="uuid-1", source="startup", cwd="/proj"
        )

        assert ok1 is True
        assert ok2 is True
        assert orch._pane_state[_key("backend")].session_uuid == "uuid-1"

    def test_lead_role_updates_lead_pane_state(self, orch: Orchestrator) -> None:
        ok, _ = orch.consume_session_report(
            "lead", project=TEST_PROJECT, session_id="lead-uuid", source="clear", cwd="/lead-cwd"
        )

        assert ok is True
        assert orch._pane_state[_key("lead")].session_uuid == "lead-uuid"

    def test_different_roles_isolated(self, orch: Orchestrator) -> None:
        orch.consume_session_report(
            "backend", project=TEST_PROJECT, session_id="backend-uuid", cwd="/proj"
        )
        orch.consume_session_report(
            "frontend", project=TEST_PROJECT, session_id="frontend-uuid", cwd="/proj"
        )

        assert orch._pane_state[_key("backend")].session_uuid == "backend-uuid"
        assert orch._pane_state[_key("frontend")].session_uuid == "frontend-uuid"


class TestPropagatesToLivePane:
    """Issue #129: PaneState.session_uuid isn't what the token meter reads —
    AgentPaneModel.session_uuid is, a separate copy stamped at attach time.
    Without pushing this SessionStart-hook update onto the live pane too, a
    manual /resume or /clear inside the pane would leave the meter polling
    the pre-rollover (now-stale) uuid's file forever."""

    def test_updates_live_pane_model_session_uuid(self, orch: Orchestrator) -> None:
        pane = MagicMock()
        pane.model.session_uuid = "spawn-time-uuid"
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane

        ok, _ = orch.consume_session_report(
            "backend",
            project=TEST_PROJECT,
            session_id="resumed-uuid",
            source="resume",
            cwd="/proj",
        )

        assert ok is True
        pane.model.set_session_uuid.assert_called_once_with("resumed-uuid")

    def test_no_open_pane_does_not_raise(self, orch: Orchestrator) -> None:
        # No pane registered for this role/project — must fail open, not crash.
        ok, _ = orch.consume_session_report(
            "ghost-role", project=TEST_PROJECT, session_id="uuid", cwd="/proj"
        )
        assert ok is True

    def test_does_not_touch_other_projects_pane(self, orch: Orchestrator) -> None:
        other_pane = MagicMock()
        orch._panes_by_project.setdefault("other-project", {})["backend"] = other_pane

        orch.consume_session_report("backend", project=TEST_PROJECT, session_id="uuid", cwd="/proj")

        other_pane.model.set_session_uuid.assert_not_called()


class TestMissingCwdKeepsPriorValue:
    def test_empty_cwd_does_not_blank_prior_cwd(self, orch: Orchestrator) -> None:
        ps = orch._ps(_key("backend"))
        ps.session_uuid_cwd = "/prior-cwd"

        ok, _ = orch.consume_session_report(
            "backend", project=TEST_PROJECT, session_id="new-uuid", cwd=""
        )

        assert ok is True
        assert orch._pane_state[_key("backend")].session_uuid_cwd == "/prior-cwd"


class TestEdgeCasesFailOpen:
    def test_empty_session_id_rejected_without_raising(self, orch: Orchestrator) -> None:
        ok, msg = orch.consume_session_report("backend", project=TEST_PROJECT, session_id="")

        assert ok is False
        assert "session_id" in msg

    def test_invalid_role_rejected_without_raising(self, orch: Orchestrator) -> None:
        ok, _msg = orch.consume_session_report(
            "../etc/passwd", project=TEST_PROJECT, session_id="abc"
        )

        assert ok is False

    def test_no_prior_pane_state_creates_fresh_entry(self, orch: Orchestrator) -> None:
        assert _key("brandnew") not in orch._pane_state

        ok, _ = orch.consume_session_report(
            "brandnew", project=TEST_PROJECT, session_id="fresh-uuid", cwd="/x"
        )

        assert ok is True
        assert orch._pane_state[_key("brandnew")].session_uuid == "fresh-uuid"
