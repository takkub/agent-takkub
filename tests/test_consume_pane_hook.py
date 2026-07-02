"""Tests for Orchestrator.consume_pane_hook — the Stop/Notification hook
signal consumer and Stop-hook done-gate.

Covers the codex cross-check findings (2026-07-02,
docs/reviews/2026-07-02-claude-hooks-design-crosscheck.md): one-shot
blocking per assignment (not per Stop event), gating only a live `working`
pane with an outstanding task, and honouring the same
blocked-on-lead/rate-limit/TTY suppressions the idle watchdog uses.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator, PaneState

TEST_PROJECT = "hooktest"


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


def _key(role: str) -> str:
    return f"{TEST_PROJECT}::{role}"


def _make_pane(*, state: str = "working", alive: bool = True) -> MagicMock:
    pane = MagicMock()
    pane.state = state
    pane.session = MagicMock()
    pane.session.is_alive = alive
    return pane


def _assign_task(orch: Orchestrator, role: str, task: str = "do the thing") -> PaneState:
    ps = orch._ps(_key(role))
    ps.last_assigned_task = task
    ps.stop_gate_notified = False
    return ps


class TestLeadNeverBlocks:
    def test_lead_stop_with_pending_task_never_blocks(self, orch: Orchestrator) -> None:
        orch.panes["lead"] = _make_pane(state="working")
        _assign_task(orch, "lead")

        ok, block, reason = orch.consume_pane_hook("lead", project=TEST_PROJECT, event="Stop")

        assert ok is True
        assert block is False
        assert reason == ""


class TestDoneGateBlocksOnce:
    def test_blocks_when_task_outstanding(self, orch: Orchestrator) -> None:
        orch.panes["backend"] = _make_pane(state="working")
        _assign_task(orch, "backend")

        ok, block, reason = orch.consume_pane_hook("backend", project=TEST_PROJECT, event="Stop")

        assert ok is True
        assert block is True
        assert "takkub done" in reason

    def test_one_shot_second_stop_does_not_block_again(self, orch: Orchestrator) -> None:
        orch.panes["backend"] = _make_pane(state="working")
        _assign_task(orch, "backend")

        first = orch.consume_pane_hook("backend", project=TEST_PROJECT, event="Stop")
        second = orch.consume_pane_hook("backend", project=TEST_PROJECT, event="Stop")

        assert first[1] is True
        assert second[1] is False, "a fresh Stop event must not re-block within the same assignment"

    def test_new_assign_resets_one_shot_budget(self, orch: Orchestrator) -> None:
        orch.panes["backend"] = _make_pane(state="working")
        _assign_task(orch, "backend", task="first task")
        orch.consume_pane_hook("backend", project=TEST_PROJECT, event="Stop")

        # A brand-new assign() (simulated directly on PaneState, mirroring
        # what assign() does) must grant a fresh one-shot budget.
        _assign_task(orch, "backend", task="second task")
        _, block, _ = orch.consume_pane_hook("backend", project=TEST_PROJECT, event="Stop")

        assert block is True

    def test_no_outstanding_task_never_blocks(self, orch: Orchestrator) -> None:
        orch.panes["backend"] = _make_pane(state="working")
        # No assign() ever happened — no PaneState / no last_assigned_task.

        ok, block, _ = orch.consume_pane_hook("backend", project=TEST_PROJECT, event="Stop")

        assert ok is True
        assert block is False

    def test_notification_event_never_blocks(self, orch: Orchestrator) -> None:
        orch.panes["backend"] = _make_pane(state="working")
        _assign_task(orch, "backend")

        _, block, _ = orch.consume_pane_hook("backend", project=TEST_PROJECT, event="Notification")

        assert block is False


class TestDoneGateSuppressions:
    def test_not_working_state_suppresses_block(self, orch: Orchestrator) -> None:
        orch.panes["backend"] = _make_pane(state="done")
        _assign_task(orch, "backend")

        _, block, _ = orch.consume_pane_hook("backend", project=TEST_PROJECT, event="Stop")

        assert block is False

    def test_dead_session_suppresses_block(self, orch: Orchestrator) -> None:
        orch.panes["backend"] = _make_pane(state="working", alive=False)
        _assign_task(orch, "backend")

        _, block, _ = orch.consume_pane_hook("backend", project=TEST_PROJECT, event="Stop")

        assert block is False

    def test_blocked_on_lead_suppresses_block(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(orch_mod.time, "time", lambda: 10_000.0)
        orch.panes["backend"] = _make_pane(state="working")
        ps = _assign_task(orch, "backend")
        ps.blocked_on_lead_ts = 10_000.0 - 60  # 1 minute ago, well inside 30-min window

        _, block, _ = orch.consume_pane_hook("backend", project=TEST_PROJECT, event="Stop")

        assert block is False

    def test_blocked_on_lead_expired_allows_block(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(orch_mod.time, "time", lambda: 10_000.0)
        orch.panes["backend"] = _make_pane(state="working")
        ps = _assign_task(orch, "backend")
        ps.blocked_on_lead_ts = 10_000.0 - (31 * 60)  # expired

        _, block, _ = orch.consume_pane_hook("backend", project=TEST_PROJECT, event="Stop")

        assert block is True

    def test_rate_limited_suppresses_block(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(orch_mod.time, "time", lambda: 10_000.0)
        orch.panes["backend"] = _make_pane(state="working")
        ps = _assign_task(orch, "backend")
        ps.rate_limited_until = 10_000.0 + 60  # resets in the future

        _, block, _ = orch.consume_pane_hook("backend", project=TEST_PROJECT, event="Stop")

        assert block is False

    def test_tty_blocked_suppresses_block(self, orch: Orchestrator) -> None:
        orch.panes["backend"] = _make_pane(state="working")
        ps = _assign_task(orch, "backend")
        ps.tty_blocked_since = 12345.0

        _, block, _ = orch.consume_pane_hook("backend", project=TEST_PROJECT, event="Stop")

        assert block is False


class TestIdleStateSignalIdempotency:
    def test_first_idle_ts_set_once(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = [1000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])
        orch.panes["backend"] = _make_pane(state="working")

        orch.consume_pane_hook("backend", project=TEST_PROJECT, event="Stop")
        assert orch._idle_state[_key("backend")]["first_idle_ts"] == 1000.0

        # A later hook firing (e.g. Notification) must not push the timestamp
        # forward — duplicate/near-simultaneous signals from hook + PTY-
        # scraping must be idempotent, not additive.
        clock[0] = 1050.0
        orch.consume_pane_hook("backend", project=TEST_PROJECT, event="Notification")
        assert orch._idle_state[_key("backend")]["first_idle_ts"] == 1000.0

    def test_lead_event_does_not_touch_idle_state(self, orch: Orchestrator) -> None:
        orch.consume_pane_hook("lead", project=TEST_PROJECT, event="Stop")
        assert _key("lead") not in orch._idle_state
