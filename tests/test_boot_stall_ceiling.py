"""Targeted tests for #276 — a pane that never leaves its boot phase must
FAIL its task out loud instead of silently swallowing it.

What happened: the delivery poll saw the provider's startup marker on screen
continuously, correctly refused to blind-paste into a splash screen that has
no composer, and then kept waiting — up to `BUSY_WAIT_CEILING_SEC`, 30 minutes
— before blind-pasting anyway. So the task was neither delivered nor failed.
Lead closed the pane by hand at ~110-180s, a report belonging to a DIFFERENT
task arrived afterwards, and the work was marked done having never run.

The ceiling is now `BOOT_STALL_CEILING_SEC` and it ends in an explicit
failure: ledger row flipped, blocking notice to Lead, pane torn down.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator

PROJECT = "bootstall"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    o = Orchestrator.__new__(Orchestrator)
    QObject.__init__(o)
    o._panes_by_project = {PROJECT: {}}
    monkeypatch.setattr(o, "_resolve_project", lambda p=None: p or PROJECT)
    monkeypatch.setattr(
        o, "_project_panes", lambda p=None: o._panes_by_project.get(o._resolve_project(p), {})
    )
    return o


def test_ceiling_is_far_below_the_busy_wait_ceiling() -> None:
    """The whole point of #276: a pane that has not started working must not
    be given the same 30-minute rope as one that is working hard."""
    assert orch_mod.BOOT_STALL_CEILING_SEC < orch_mod.BUSY_WAIT_CEILING_SEC
    # ...but still past every provider's own cold-boot allowance plus codex's
    # measured ~61s baseline for a trivial prompt (#278).
    assert orch_mod.BOOT_STALL_CEILING_SEC > orch_mod.BOOT_STALL_GRACE_SEC


def test_boot_timeout_reports_a_failure_through_done(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(
        orch,
        "done",
        lambda role, note="", project=None, failed=False, force=False: (
            calls.append({"role": role, "note": note, "failed": failed, "force": force})
            or (True, "ok")
        ),
    )
    orch._fail_boot_stalled_delivery("frontend", PROJECT, 305.0)

    assert len(calls) == 1
    call = calls[0]
    assert call["failed"] is True, "the task must end as FAILED, not quietly"
    assert call["force"] is True, "the pane never got the task — the #278 gate would refuse it"
    assert "boot-timeout" in call["note"]
    # Lead must be told the work never started, NOT that it was attempted and
    # went wrong — those need different responses.
    assert "ยังไม่ได้เริ่มทำ" in call["note"]
    assert "takkub task show --role frontend" in call["note"], "the task text must be recoverable"


def test_report_failure_still_reaches_lead_if_done_raises(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure path itself must never be the thing that goes silent."""

    def _boom(*a, **k):
        raise RuntimeError("teardown exploded")

    notices: list[str] = []
    monkeypatch.setattr(orch, "done", _boom)
    monkeypatch.setattr(orch, "_notify_lead", lambda p, body, **kw: notices.append(body) or None)
    orch._fail_boot_stalled_delivery("frontend", PROJECT, 305.0)

    assert notices and "FAILED" in notices[0]


def test_boot_stall_warning_does_not_assert_mcp_as_the_cause(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#278 measured codex taking 61s on a trivial prompt with NO MCP servers
    configured, so "loading MCP server" was a cause the cockpit had invented.
    It may only report what it observed: the startup marker is still up.

    Driven under the `live` policy because #280 no longer sends this the
    moment it is noticed — the wording is unchanged, only its timing is (see
    test_pane_health_reporting.py). `live` is where that wording is still
    rendered verbatim, so this is the honest place to assert on it.
    """
    monkeypatch.setenv("TAKKUB_PANE_WATCH_NOTICES", "live")
    lead = MagicMock()
    lead.session = MagicMock()
    lead.session.is_alive = True
    orch._panes_by_project[PROJECT] = {"lead": lead, "frontend": MagicMock()}
    bodies: list[str] = []
    monkeypatch.setattr(orch, "_notify_lead", lambda p, body, **kw: bodies.append(body) or None)
    monkeypatch.setattr(orch, "_run_boot_diagnostic_async", lambda *a, **k: None)

    orch._warn_lead_delivery_boot_stall("frontend", PROJECT, 120.0)

    assert bodies
    assert "MCP server" not in bodies[0]
    assert "startup marker" in bodies[0]
    # and it must promise the automatic resolution, so Lead stops babysitting
    assert "FAILED" in bodies[0]


def test_boot_stall_is_not_narrated_live_by_default(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#280: by default the slow boot waits for the pane's own report."""
    monkeypatch.delenv("TAKKUB_PANE_WATCH_NOTICES", raising=False)
    lead = MagicMock()
    lead.session = MagicMock()
    lead.session.is_alive = True
    orch._panes_by_project[PROJECT] = {"lead": lead, "frontend": MagicMock()}
    bodies: list[str] = []
    monkeypatch.setattr(orch, "_notify_lead", lambda p, body, **kw: bodies.append(body) or None)
    monkeypatch.setattr(orch, "_run_boot_diagnostic_async", lambda *a, **k: None)

    orch._warn_lead_delivery_boot_stall("frontend", PROJECT, 120.0)

    assert bodies == []
    assert "boot ช้า 120s" in orch._drain_pane_health(PROJECT, "frontend")
