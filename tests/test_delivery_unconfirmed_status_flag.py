"""Targeted tests for #263's safely-additive slice: `list_status_detailed`
surfaces `delivery_unconfirmed` so a role that reads "working" but whose
task may not have actually landed (spawn-failed/delivery-unconfirmed/
spawn-stuck/delivery-boot-stall) no longer looks identical to a role that
is genuinely running.

Real evidence: a gemini pane read "working" from minute one while its
screen was stuck on "Signing in..." and the task was never pasted in, with
the cockpit's own `[delivery-unconfirmed]` notice sitting unread. Full
unification of declared-state/ready-marker/progress-clock (#263's larger
ask) is out of scope here — it would mean touching provider-specific
ready-marker calibration (provider_spec.py / pty_section fixtures) reserved
for a parallel fix; see docs/audit/2026-08-16-263-264-266-notify-truth.md
for the scope decision.
"""

from __future__ import annotations

import collections
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub.orchestrator import Orchestrator

PROJECT = "delivery-flag-test"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _pane(state: str = "working", alive: bool = True) -> MagicMock:
    p = MagicMock()
    p.state = state
    p.session = MagicMock()
    p.session.is_alive = alive
    return p


@pytest.fixture
def orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    o = Orchestrator.__new__(Orchestrator)
    QObject.__init__(o)
    o._panes_by_project = {}
    o._pane_state = {}
    o._lead_digest_queue = {}
    o._lead_notify_queue = {}
    o._pending_done_notices = {}
    o._recent_done = []
    monkeypatch.setattr(o, "_resolve_project", lambda p=None: p or PROJECT)
    monkeypatch.setattr(
        o, "_project_panes", lambda p=None: o._panes_by_project.get(o._resolve_project(p), {})
    )
    return o


def test_flag_true_when_delivery_unconfirmed_notice_pending(orch: Orchestrator) -> None:
    orch._panes_by_project[PROJECT] = {"gemini": _pane("working")}
    orch._lead_digest_queue[PROJECT] = collections.deque()
    orch._lead_notify_queue[PROJECT] = collections.deque(
        [("⚠️ [delivery-unconfirmed] gemini pane ไม่ถึง ready prompt ใน 90s", None)]
    )

    detailed = orch.list_status_detailed(project=PROJECT)

    assert detailed["gemini"]["state"] == "working"
    assert detailed["gemini"]["delivery_unconfirmed"] is True


def test_flag_false_when_no_notice_pending(orch: Orchestrator) -> None:
    orch._panes_by_project[PROJECT] = {"backend": _pane("working")}
    orch._lead_digest_queue[PROJECT] = collections.deque()
    orch._lead_notify_queue[PROJECT] = collections.deque()

    detailed = orch.list_status_detailed(project=PROJECT)

    assert detailed["backend"]["delivery_unconfirmed"] is False


def test_flag_false_for_a_different_roles_notice(orch: Orchestrator) -> None:
    """Only the NAMED role's own notice sets the flag — a queued warning
    about a different role must not leak onto this one."""
    orch._panes_by_project[PROJECT] = {"backend": _pane("working")}
    orch._lead_notify_queue[PROJECT] = collections.deque(
        [("⚠️ [spawn-stuck] frontend assign ค้างใน spawn queue นานเกิน 90s", None)]
    )

    detailed = orch.list_status_detailed(project=PROJECT)

    assert detailed["backend"]["delivery_unconfirmed"] is False


def test_delivery_busy_wait_does_not_set_the_flag(orch: Orchestrator) -> None:
    """delivery-busy-wait means delivery DID land (pane's just slow) — a
    different claim from "may not have landed", so it must not set this
    flag (unlike #266's broader revalidation marker set, which
    deliberately DOES include busy-wait)."""
    orch._panes_by_project[PROJECT] = {"kimi": _pane("working")}
    orch._lead_notify_queue[PROJECT] = collections.deque(
        [("⏳ [delivery-busy-wait] kimi pane ยังไม่ถึง ready prompt แต่มี output ล่าสุดเมื่อ 3s", None)]
    )

    detailed = orch.list_status_detailed(project=PROJECT)

    assert detailed["kimi"]["delivery_unconfirmed"] is False


def test_flag_absent_for_non_working_pane(orch: Orchestrator) -> None:
    """The check only runs for a pane that currently reads "working" — a
    stale notice for an already-closed role shouldn't resurrect it here
    (that's `_pending_notice_roles`'s job, a separate mechanism)."""
    orch._panes_by_project[PROJECT] = {"backend": _pane("done")}

    detailed = orch.list_status_detailed(project=PROJECT)

    assert detailed["backend"]["delivery_unconfirmed"] is False


def test_checking_status_does_not_mark_the_notice_as_read(orch: Orchestrator) -> None:
    """Regression guard: an earlier draft of this fix reused `inbox_report`,
    whose fingerprinting side effect silently marked every currently-queued
    notice as "already read via takkub inbox" purely because Lead called
    `takkub status` — collapsing it once it actually flushed, even though
    Lead never explicitly pulled it. `list_status_detailed` must be
    read-only."""
    orch._panes_by_project[PROJECT] = {
        "lead": _pane("working"),
        "backend": _pane("working"),
    }
    orch._lead_digest_queue[PROJECT] = collections.deque(
        [("[backend done] shipped it", None, 0.0, None)]
    )

    orch.list_status_detailed(project=PROJECT)

    assert getattr(orch, "_inbox_seen", {}).get(PROJECT, set()) == set()
