"""Issue #543: an `assign()` held behind the resource governor's machine-wide
overload latch (CPU/RAM, not a per-class slot wait) used to be silent — the
"queued — machine overloaded (...)" message only ever reached whoever called
`assign()` synchronously. Most real calls go through `cli_server.py`'s
`_fire_staggered` (QTimer-deferred, ack already sent before `assign()` runs),
so the hold reason was dropped on the floor and Lead only found out by
manually running `takkub status` (the issue's exact repro).

`assign()` must now push the same message into Lead's inbox via
`_notify_lead` at the moment it decides to hold, regardless of who is
listening for its return value.
"""

from __future__ import annotations

import pathlib
from unittest.mock import patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub import orchestrator as orch_mod
from agent_takkub import task_ledger
from agent_takkub.orchestrator import Orchestrator
from agent_takkub.resource_governor import GovernorLimits, ResourceClass, ResourceGovernor

PROJECT = "proj"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
    monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)
    monkeypatch.setattr(task_ledger, "RUNTIME_DIR", tmp_path)

    with patch("agent_takkub.orchestrator.Orchestrator._load_pending_cc", lambda self: None):
        o = Orchestrator.__new__(Orchestrator)
        QObject.__init__(o)
        o._panes_by_project = {}
        o._pane_state = {}
        o._session_goals = {}
        o._resource_tokens = {}
        o._pending_lead_cc = {}
        o._pending_done_notices = {}
        o._resource_governor = ResourceGovernor(
            GovernorLimits(
                max_heavy_global=4,
                max_heavy_per_project=2,
                max_browser_global=1,
                max_build_global=2,
                max_test_global=2,
                max_package_install_global=1,
                cpu_pause_percent=85,
                cpu_resume_percent=65,
                min_available_ram_percent=20,
                resume_ram_percent=25,
            )
        )
    return o


def _force_overload(orch: Orchestrator, *, cpu: float, ram_free: float) -> None:
    governor = orch._resource_governor
    with governor._lock:
        governor._overloaded = True
        governor._cpu_percent = cpu
        governor._available_ram_percent = ram_free


class TestOverloadHoldNotifiesLead:
    def test_memory_low_hold_pushes_a_notice_into_leads_inbox(self, orch: Orchestrator) -> None:
        _force_overload(orch, cpu=46.0, ram_free=9.0)

        # "qa" classifies as ResourceClass.BROWSER (see classify_resource) —
        # not LIGHT/NORMAL, so the overload latch denies it directly instead
        # of falling through to the separate backpressure ladder.
        ok, msg = orch.assign("qa#2", "/api", "some task", project=PROJECT)

        assert ok is True
        assert "queued" in msg and "memory_low" in msg

        pending = orch._pending_done_notices.get(PROJECT, [])
        assert len(pending) == 1, "the overload hold must be pushed to Lead's inbox, not dropped"
        assert "memory_low" in pending[0]["body"]
        assert "queued" in pending[0]["body"]
        assert pending[0]["note"] == "resource-overload-queued"

    def test_notice_reaches_lead_even_when_return_value_is_discarded(
        self, orch: Orchestrator
    ) -> None:
        """Mirrors the real `cli_server.py` path: the caller never reads
        `assign()`'s return value (it's fired off a QTimer whose ack was
        already sent), yet Lead must still learn about the hold."""
        _force_overload(orch, cpu=46.0, ram_free=9.0)

        orch.assign("qa#2", "/api", "some task", project=PROJECT)  # return value ignored

        assert orch._pending_done_notices.get(PROJECT), "notice must land regardless of caller"

    def test_ordinary_per_class_slot_wait_does_not_double_up_with_a_notify(
        self, orch: Orchestrator
    ) -> None:
        """Scoped fix: a plain per-class slot wait (not the overload latch)
        already names its blocking pane in `takkub list`/`status` — it must
        NOT also page Lead's inbox, or every ordinary queue would become
        inbox noise."""
        # Occupy the sole browser slot directly, then queue a second
        # browser-class role behind it — a `browser_global_limit` reason,
        # not an overload-latch reason.
        held = orch._resource_governor.request_slot(
            project_id=PROJECT,
            pane_id="qa#1",
            task_id="t-held",
            resource_class=ResourceClass.BROWSER,
        )
        assert held.allowed

        ok, msg = orch.assign("qa#2", "/api", "some task", project=PROJECT)

        assert ok is True
        assert "browser_global_limit" in msg
        assert not orch._pending_done_notices.get(PROJECT), (
            "an ordinary slot-limit wait must not also push an inbox notice"
        )
