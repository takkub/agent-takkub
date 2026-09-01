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
    o.shutdown_timers()
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

    def test_progress_call_suppresses_the_next_stop(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#461: `takkub progress` (e.g. reporting it's waiting on
        credentials from Lead) must let the very next Stop event end the
        turn instead of forcing `takkub done` — same 30-min grace the
        blocked-on-lead suppression above already gives a direct
        `takkub send --to lead`."""
        monkeypatch.setattr(orch_mod.time, "time", lambda: 10_000.0)
        orch.panes["backend"] = _make_pane(state="working")
        _assign_task(orch, "backend")

        ok, _msg = orch.progress("backend", note="waiting on credentials", project=TEST_PROJECT)
        assert ok is True

        _, block, _ = orch.consume_pane_hook("backend", project=TEST_PROJECT, event="Stop")

        assert block is False

    def test_progress_grace_expires_then_blocks_again(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The flip side of #461: a pane that reported progress once but then
        goes silent past the grace window must still get nudged — progress()
        is not a permanent done-gate bypass."""
        clock = [10_000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])
        orch.panes["backend"] = _make_pane(state="working")
        _assign_task(orch, "backend")
        orch.progress("backend", note="waiting on credentials", project=TEST_PROJECT)

        clock[0] = 10_000.0 + (31 * 60)  # past the 30-min grace window
        _, block, _ = orch.consume_pane_hook("backend", project=TEST_PROJECT, event="Stop")

        assert block is True


class TestLastTurnEndTsStamping:
    """#463 follow-up: a non-blocking Stop hook is the only proof a turn
    genuinely ended — `last_turn_end_ts` must be stamped on every
    `(ok=True, block=False)` return for a real Stop event, and left alone
    (None) whenever the hook actually blocks (the turn is being forced to
    continue, not ending)."""

    def test_non_blocking_stop_stamps_last_turn_end_ts(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(orch_mod.time, "time", lambda: 10_000.0)
        orch.panes["backend"] = _make_pane(state="working")
        ps = _assign_task(orch, "backend")
        ps.blocked_on_lead_ts = 10_000.0 - 60  # inside the 30-min grace window

        _, block, _ = orch.consume_pane_hook("backend", project=TEST_PROJECT, event="Stop")

        assert block is False
        assert ps.last_turn_end_ts == 10_000.0

    def test_blocking_stop_does_not_stamp_last_turn_end_ts(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The done-gate nudge means the turn is being forced to continue
        # (Claude Code is told to keep going), not that it genuinely ended.
        monkeypatch.setattr(orch_mod.time, "time", lambda: 10_000.0)
        orch.panes["backend"] = _make_pane(state="working")
        ps = _assign_task(orch, "backend")

        _, block, _ = orch.consume_pane_hook("backend", project=TEST_PROJECT, event="Stop")

        assert block is True
        assert ps.last_turn_end_ts is None

    def test_no_outstanding_task_still_stamps_last_turn_end_ts(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No assign() ever happened, but `_ps()` lazily creates a PaneState
        # for the role the first time anything touches it (e.g. spawn) — if
        # one already exists, a genuinely-ended turn is still real evidence
        # even with no outstanding task.
        monkeypatch.setattr(orch_mod.time, "time", lambda: 10_000.0)
        orch.panes["backend"] = _make_pane(state="working")
        ps = orch._ps(_key("backend"))

        _, block, _ = orch.consume_pane_hook("backend", project=TEST_PROJECT, event="Stop")

        assert block is False
        assert ps.last_turn_end_ts == 10_000.0

    def test_notification_event_does_not_stamp_last_turn_end_ts(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Notification is not a turn-end signal — only Stop is.
        monkeypatch.setattr(orch_mod.time, "time", lambda: 10_000.0)
        orch.panes["backend"] = _make_pane(state="working")
        ps = _assign_task(orch, "backend")

        orch.consume_pane_hook("backend", project=TEST_PROJECT, event="Notification")

        assert ps.last_turn_end_ts is None

    def test_progress_mid_turn_with_no_stop_hook_yet_leaves_last_turn_end_ts_unset(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end #463 follow-up scenario 1: `progress()` is called mid-
        turn and the pane keeps working — no Stop hook follows. `waiting-lead`
        needs `last_turn_end_ts`, and it must stay unset."""
        monkeypatch.setattr(orch_mod.time, "time", lambda: 10_000.0)
        orch.panes["backend"] = _make_pane(state="working")
        ps = _assign_task(orch, "backend")

        ok, _ = orch.progress("backend", note="กำลังแก้ X", project=TEST_PROJECT)

        assert ok is True
        assert ps.blocked_on_lead_ts == 10_000.0
        assert ps.last_turn_end_ts is None

    def test_progress_then_stop_hook_makes_last_turn_end_ts_catch_up(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end #463 follow-up scenario 2: `progress()` mid-turn,
        followed later by a genuine (non-blocking) Stop hook — the turn has
        now really ended, so `last_turn_end_ts` must catch up to (or pass)
        `blocked_on_lead_ts`, satisfying the `waiting-lead` tier's ordering
        check in `_derive_display_state`."""
        clock = [10_000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])
        orch.panes["backend"] = _make_pane(state="working")
        ps = _assign_task(orch, "backend")

        orch.progress("backend", note="กำลังแก้ X", project=TEST_PROJECT)
        clock[0] = 10_000.0 + 12 * 60  # 12 more minutes of real work, then it stops
        _, block, _ = orch.consume_pane_hook("backend", project=TEST_PROJECT, event="Stop")

        assert block is False
        assert ps.last_turn_end_ts == clock[0]
        assert ps.last_turn_end_ts >= ps.blocked_on_lead_ts

    def test_stop_hook_before_a_fresh_progress_call_does_not_predate_it(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end #463 follow-up scenario 3: a Stop hook ends one turn
        (stamping `last_turn_end_ts`), Lead sends new instructions (clearing
        both fields — new one-shot budget), and the pane's fresh turn calls
        `progress()` again. The old `last_turn_end_ts` must not leak forward
        and satisfy the new `blocked_on_lead_ts` — it's None again until a
        Stop hook fires for THIS turn."""
        clock = [10_000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])
        orch.panes["backend"] = _make_pane(state="working")
        ps = _assign_task(orch, "backend")

        orch.progress("backend", note="รอบแรก", project=TEST_PROJECT)
        clock[0] = 10_000.0 + 60
        orch.consume_pane_hook("backend", project=TEST_PROJECT, event="Stop")
        assert ps.last_turn_end_ts == clock[0]

        # Lead replies with new instructions — clears both fields (orchestrator.py's
        # send() path) — and the pane starts a fresh turn, reporting progress again.
        clock[0] = 10_000.0 + 120
        orch.send("backend", "ลองอีกทีนะ", from_role="lead", project=TEST_PROJECT)
        assert ps.blocked_on_lead_ts is None
        assert ps.last_turn_end_ts is None

        clock[0] = 10_000.0 + 180
        _assign_task(orch, "backend")
        orch.progress("backend", note="รอบสอง", project=TEST_PROJECT)

        assert ps.blocked_on_lead_ts == clock[0]
        assert ps.last_turn_end_ts is None


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
