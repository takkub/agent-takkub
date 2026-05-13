"""Tests for the idle watchdog that reminds teammates to call `takkub done`.

The watchdog lives in Orchestrator. We avoid spinning up the real Qt event
loop by stopping the watchdog QTimer immediately after construction and
driving `_check_idle_teammates()` manually with a mocked clock.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    """A single QCoreApplication for the module — required by QObject/QTimer."""
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp: QCoreApplication) -> Orchestrator:
    o = Orchestrator()
    # We drive _check_idle_teammates by hand; the auto-firing timer would
    # race against our assertions.
    o._idle_watchdog.stop()
    return o


def _make_pane(
    *,
    state: str = "working",
    alive: bool = True,
    at_ready_prompt: bool = True,
) -> MagicMock:
    pane = MagicMock()
    pane.state = state
    pane.session = MagicMock()
    pane.session.is_alive = alive
    pane.session.is_at_ready_prompt.return_value = at_ready_prompt
    return pane


class TestIdleWatchdog:
    def test_idle_streak_starts_when_pane_is_idle(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pane = _make_pane(state="working", at_ready_prompt=True)
        orch.panes["backend"] = pane

        monkeypatch.setattr(orch_mod.time, "time", lambda: 1000.0)
        orch._check_idle_teammates()

        assert orch._idle_state["backend"]["first_idle_ts"] == 1000.0
        # Not enough time yet — no reminder fired.
        pane.session.write.assert_not_called()

    def test_reminder_fires_after_threshold(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pane = _make_pane(state="working", at_ready_prompt=True)
        orch.panes["backend"] = pane

        clock = [1000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])

        orch._check_idle_teammates()  # start the streak
        clock[0] += orch_mod.IDLE_REMIND_AFTER_S + 1  # cross threshold
        orch._check_idle_teammates()

        pane.session.write.assert_called_with(orch_mod.IDLE_REMINDER_TEXT)
        assert orch._idle_state["backend"]["last_reminder_ts"] == clock[0]

    def test_processing_pane_resets_streak(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pane = _make_pane(state="working", at_ready_prompt=True)
        orch.panes["backend"] = pane

        clock = [1000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])

        orch._check_idle_teammates()
        assert orch._idle_state["backend"]["first_idle_ts"] == 1000.0

        # Pane goes back to processing (long shell command) — streak resets.
        pane.session.is_at_ready_prompt.return_value = False
        clock[0] += 10
        orch._check_idle_teammates()
        assert orch._idle_state["backend"]["first_idle_ts"] is None

        # When it comes back to idle, the streak restarts from scratch.
        pane.session.is_at_ready_prompt.return_value = True
        clock[0] += 5
        orch._check_idle_teammates()
        assert orch._idle_state["backend"]["first_idle_ts"] == clock[0]

    def test_lead_pane_is_never_reminded(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Even if Lead somehow ended up in "working" with an idle prompt,
        # the watchdog must skip it.
        lead = _make_pane(state="working", at_ready_prompt=True)
        orch.panes["lead"] = lead

        clock = [1000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])

        orch._check_idle_teammates()
        clock[0] += orch_mod.IDLE_REMIND_AFTER_S + 10
        orch._check_idle_teammates()

        lead.session.write.assert_not_called()
        assert "lead" not in orch._idle_state

    def test_non_working_state_clears_tracking(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pane = _make_pane(state="working", at_ready_prompt=True)
        orch.panes["backend"] = pane

        clock = [1000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])

        orch._check_idle_teammates()
        assert "backend" in orch._idle_state

        pane.state = "done"  # agent finally called `takkub done`
        orch._check_idle_teammates()
        assert "backend" not in orch._idle_state
        pane.session.write.assert_not_called()

    def test_dead_session_clears_tracking(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pane = _make_pane(state="working", at_ready_prompt=True, alive=False)
        orch.panes["backend"] = pane

        monkeypatch.setattr(orch_mod.time, "time", lambda: 1000.0)
        orch._check_idle_teammates()

        assert "backend" not in orch._idle_state
        pane.session.write.assert_not_called()

    def test_cooldown_prevents_back_to_back_reminders(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pane = _make_pane(state="working", at_ready_prompt=True)
        orch.panes["backend"] = pane

        clock = [1000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])

        # First reminder fires.
        orch._check_idle_teammates()
        clock[0] += orch_mod.IDLE_REMIND_AFTER_S + 1
        orch._check_idle_teammates()
        assert pane.session.write.call_count == 1

        # Inside the cooldown window — even sitting idle, no second nag.
        clock[0] += orch_mod.IDLE_REMIND_AFTER_S + 1
        orch._check_idle_teammates()
        assert pane.session.write.call_count == 1

        # After cooldown elapses we're willing to nudge again.
        clock[0] += orch_mod.IDLE_REMIND_COOLDOWN_S + 1
        orch._check_idle_teammates()
        assert pane.session.write.call_count == 2


class TestIdleResetHooks:
    """`done`, `spawn`, `close` should each clear stale idle tracking so a
    pane that completed (or was killed) doesn't keep firing reminders."""

    def test_done_clears_idle_state(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pane = _make_pane(state="working", at_ready_prompt=True)
        orch.panes["backend"] = pane
        # Stub out the Lead pane so `done()` can write the notice.
        orch.panes["lead"] = _make_pane(state="active")
        orch._idle_state["backend"] = {"first_idle_ts": 1.0, "last_reminder_ts": 2.0}

        # Suppress Qt timer-based auto-close inside done().
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", lambda *_args, **_kw: None)

        ok, _ = orch.done("backend", note="green")
        assert ok
        assert "backend" not in orch._idle_state

    def test_close_clears_idle_state(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pane = _make_pane(state="working", at_ready_prompt=True)
        orch.panes["backend"] = pane
        orch._idle_state["backend"] = {"first_idle_ts": 1.0, "last_reminder_ts": 2.0}

        # paneClosed.emit happens in close(); the signal has no slots in tests
        # so it just no-ops. mark_expected_exit + terminate are mocks.
        orch.close("backend")
        assert "backend" not in orch._idle_state


def test_signal_emit_no_op_when_disabled(qapp: QCoreApplication) -> None:
    """When IDLE_REMIND_AFTER_S is set to 0 the watchdog must not auto-start.

    Guards against accidental activation during tests / debugging."""
    import importlib

    o = Orchestrator()
    try:
        o._idle_watchdog.stop()
        assert not o._idle_watchdog.isActive()
    finally:
        del o
    importlib.reload(orch_mod)  # restore module-level constants
