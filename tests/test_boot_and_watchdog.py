"""Tests for boot auto-advance and task-start watchdog."""

from unittest.mock import MagicMock, patch

import pytest

from agent_takkub.orchestrator import Orchestrator
from agent_takkub.provider_spec import PROVIDER_REGISTRY


@pytest.fixture
def orch(monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(Orchestrator, "_resolve_project", lambda self, p: p or "proj")
    orch = Orchestrator()
    orch._idle_watchdog.stop()
    return orch


def test_boot_auto_advance_whitelist_and_bound(orch: Orchestrator) -> None:
    pane = MagicMock()
    pane.role.provider = "claude"
    pane.session.is_alive = True
    pane.session.is_at_ready_prompt.return_value = False
    orch._panes_by_project["proj"] = {"backend": pane}

    pane.session.display_lines.return_value = ["trust this folder"]

    with (
        patch("agent_takkub.spawn_engine.QTimer") as mock_timer,
        patch("agent_takkub.spawn_engine._log_event") as mock_log,
    ):
        orch._boot_auto_advance("backend", project="proj")
        check_func = mock_timer.singleShot.call_args[0][1]

        for _ in range(6):
            check_func()

        assert pane.session.write.call_count == 5
        assert mock_log.call_count == 5
        mock_log.assert_called_with(
            "boot_auto_advance", role="backend", project="proj", marker="trust this folder"
        )


def test_boot_auto_advance_unknown(orch: Orchestrator) -> None:
    pane = MagicMock()
    pane.role.provider = "claude"
    pane.session.is_alive = True
    pane.session.is_at_ready_prompt.return_value = False
    orch._panes_by_project["proj"] = {"backend": pane}

    import dataclasses

    with patch.dict(PROVIDER_REGISTRY, {}):
        base_spec = PROVIDER_REGISTRY["claude"]
        new_spec = dataclasses.replace(
            base_spec,
            ready_hard_blockers=("trust this folder", "alien blocker"),
            boot_auto_advance_screens=("trust this folder",),
        )
        PROVIDER_REGISTRY["claude"] = new_spec

        pane.session.display_lines.return_value = ["alien blocker"]

        orch.send = MagicMock()
        with (
            patch("agent_takkub.spawn_engine.QTimer") as mock_timer,
            patch("agent_takkub.spawn_engine._log_event") as mock_log,
        ):
            orch._boot_auto_advance("backend", project="proj")
            check_func = mock_timer.singleShot.call_args[0][1]
            check_func()

            mock_log.assert_called_with(
                "boot_auto_advance_unknown", role="backend", project="proj", marker="alien blocker"
            )
            orch.send.assert_called_once()
            assert "ติดหน้าจอที่ไม่รู้จักตอน boot (alien blocker)" in orch.send.call_args[0][1]


def test_task_start_watchdog_started(orch: Orchestrator) -> None:
    pane = MagicMock()
    pane.role.provider = "claude"
    session = MagicMock()
    session.is_alive = True
    session.shows_any_status_marker.return_value = True
    pane.session = session
    orch._panes_by_project["proj"] = {"backend": pane}

    with (
        patch("agent_takkub.lead_inbox.QTimer") as mock_timer,
        patch("agent_takkub.lead_inbox._log_event") as mock_log,
    ):
        orch._arm_task_start_watchdog("backend", "proj", session)
        check_func = mock_timer.singleShot.call_args[0][1]
        check_func()

        mock_log.assert_called_with(
            "task_started", project="proj", role="backend", provider="claude"
        )


def test_task_start_watchdog_timeout(orch: Orchestrator) -> None:
    pane = MagicMock()
    pane.role.provider = "claude"
    session = MagicMock()
    session.is_alive = True
    session.is_at_ready_prompt.return_value = True
    session.shows_any_status_marker.return_value = False
    session.display_lines.return_value = ["idle"]
    session._last_output_ts = 0
    pane.session = session
    orch._panes_by_project["proj"] = {"backend": pane}
    orch.send = MagicMock()

    with (
        patch("agent_takkub.lead_inbox.QTimer") as mock_timer,
        patch("agent_takkub.lead_inbox._log_event") as mock_log,
    ):
        orch._arm_task_start_watchdog("backend", "proj", session)
        check_func = mock_timer.singleShot.call_args[0][1]

        for _ in range(60):
            check_func()

        mock_log.assert_called_with(
            "task_start_timeout", project="proj", role="backend", provider="claude"
        )
        orch.send.assert_called_once()
        assert "ยังไม่เริ่มทำงานหลังส่ง task ไป 120 วิ" in orch.send.call_args[0][1]
