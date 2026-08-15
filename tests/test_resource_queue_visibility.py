"""Targeted tests for issue #240 point 3: `takkub assign` used to answer
`ok: task queued (spawning async, +0ms)` for a role blocked behind a
resource-governor slot, and then the role vanished from `takkub list` /
`takkub status` entirely — it has no pane entry yet, so the plain
`_project_panes()`-driven status snapshot never saw it. The only way to
notice was reading `runtime/events.log` directly.

`_queued_resource_roles` / `_describe_resource_wait` now keep a queued role
visible (with the blocking pane named) until it's actually admitted, the
same way `_pending_notice_roles` already does for issue #163.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator, _describe_resource_wait
from agent_takkub.resource_governor import GovernorLimits, ResourceClass, ResourceGovernor


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp, tmp_path, monkeypatch) -> Orchestrator:
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
    monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)
    with patch("agent_takkub.orchestrator.Orchestrator._load_pending_cc", lambda self: None):
        o = Orchestrator.__new__(Orchestrator)
        QObject.__init__(o)
        o._panes_by_project = {}
        o._pane_state = {}
        o._idle_state = {}
        o._recent_exits = {}
        o._recent_done = []
        o._pending_lead_cc = {}
        o._pending_done_notices = {}
    return o


def _limits(**overrides) -> GovernorLimits:
    values = {
        "max_heavy_global": 4,
        "max_heavy_per_project": 2,
        "max_browser_global": 2,
        "max_build_global": 2,
        "max_test_global": 2,
        "max_package_install_global": 1,
        "cpu_pause_percent": 85,
        "cpu_resume_percent": 65,
        "min_available_ram_percent": 20,
        "resume_ram_percent": 25,
    }
    values.update(overrides)
    return GovernorLimits(**values)


class TestDescribeResourceWait:
    def test_includes_reason_and_holder(self) -> None:
        msg = _describe_resource_wait(
            "backend#2",
            ResourceClass.PACKAGE_INSTALL,
            "package_install_global_limit",
            ["backend#1"],
        )
        assert msg == (
            "backend#2 queued — waiting for package_install slot "
            "(package_install_global_limit, blocked by backend#1)"
        )

    def test_omits_blocked_by_clause_when_no_holder_known(self) -> None:
        msg = _describe_resource_wait("backend#2", ResourceClass.BUILD, "cpu_high", [])
        assert msg == "backend#2 queued — waiting for build slot (cpu_high)"
        assert "blocked by" not in msg


class TestQueuedResourceRolesSurfacedInList:
    def test_queued_role_appears_in_list_status_with_blocking_pane(self, orch) -> None:
        proj = "proj"
        governor = ResourceGovernor(_limits())
        orch._resource_governor = governor
        held = governor.request_slot(
            project_id=proj,
            pane_id="backend#1",
            task_id="t-held",
            resource_class=ResourceClass.PACKAGE_INSTALL,
        )
        assert held.allowed
        governor.enqueue(
            project_id=proj,
            pane_id="backend#2",
            task_id="t-wait",
            resource_class=ResourceClass.PACKAGE_INSTALL,
            reason="package_install_global_limit",
        )

        status = orch.list_status(project=proj)
        assert "backend#2" in status, "queued role must not silently vanish (#240)"
        assert "package_install" in status["backend#2"]
        assert "blocked by backend#1" in status["backend#2"]

        detailed = orch.list_status_detailed(project=proj)
        assert detailed["backend#2"]["state"] == status["backend#2"]
        assert detailed["backend#2"]["stall_minutes"] is None

    def test_live_pane_state_wins_over_queued_synthetic_entry(self, orch) -> None:
        """If a role somehow has both a real pane AND a stale queue entry
        (e.g. it was admitted via a race but the queue hasn't drained yet),
        the real pane state must win — mirrors #163's precedence rule."""
        from unittest.mock import MagicMock

        proj = "proj"
        pane = MagicMock()
        pane.state = "working"
        orch._panes_by_project = {proj: {"backend#2": pane}}

        governor = ResourceGovernor(_limits())
        orch._resource_governor = governor
        governor.enqueue(
            project_id=proj,
            pane_id="backend#2",
            task_id="t-wait",
            resource_class=ResourceClass.PACKAGE_INSTALL,
            reason="package_install_global_limit",
        )

        assert orch.list_status(project=proj)["backend#2"] == "working"

    def test_other_project_queue_entry_not_leaked(self, orch) -> None:
        governor = ResourceGovernor(_limits())
        orch._resource_governor = governor
        governor.enqueue(
            project_id="other-proj",
            pane_id="backend#2",
            task_id="t-wait",
            resource_class=ResourceClass.PACKAGE_INSTALL,
            reason="package_install_global_limit",
        )

        assert "backend#2" not in orch.list_status(project="proj")

    def test_no_governor_yet_is_a_noop(self, orch) -> None:
        assert orch.list_status(project="proj") == {}
        assert orch.list_status_detailed(project="proj") == {}
