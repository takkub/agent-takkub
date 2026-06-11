"""Tests for the idle watchdog that reminds teammates to call `takkub done`.

The watchdog lives in Orchestrator. We avoid spinning up the real Qt event
loop by stopping the watchdog QTimer immediately after construction and
driving `_check_idle_teammates()` manually with a mocked clock.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import (
    IDLE_REMINDER_TEXT,
    TTY_BLOCK_SURFACE_AFTER_S,
    TTY_BLOCK_SURFACE_COOLDOWN_S,
    Orchestrator,
)


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    """A single QCoreApplication for the module — required by QObject/QTimer."""
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


# A stable project namespace for every test in this module. The
# orchestrator namespaces idle-state keys as "<project>::<role>" now;
# pinning the active project here keeps assertions deterministic
# regardless of whatever real projects.json the dev environment has.
TEST_PROJECT = "testproj"


@pytest.fixture
def orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    # Force every `_resolve_project(None)` call to land on TEST_PROJECT so
    # `orch.panes["backend"] = pane` writes to a known namespace and the
    # idle-state key is predictable.
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or TEST_PROJECT),
    )
    o = Orchestrator()
    # We drive _check_idle_teammates by hand; the auto-firing timer would
    # race against our assertions.
    o._idle_watchdog.stop()
    return o


def _key(role: str) -> str:
    """Idle-state key for the current test namespace."""
    return f"{TEST_PROJECT}::{role}"


def _make_pane(
    *,
    state: str = "working",
    alive: bool = True,
    at_ready_prompt: bool = True,
    tty_prompt: str | None = None,
) -> MagicMock:
    pane = MagicMock()
    pane.state = state
    pane.session = MagicMock()
    pane.session.is_alive = alive
    pane.session.is_at_ready_prompt.return_value = at_ready_prompt
    # Default: not rate-limited (real PtySession returns None when no usage-limit
    # banner is showing). Without this a MagicMock would return a truthy stub and
    # the rate-limit gate would suppress the idle reminder.
    pane.session.rate_limit_reset_at.return_value = None
    # Default: not blocked on a TTY prompt. Without this a MagicMock would return
    # a truthy stub and the TTY-block gate would suppress the idle reminder.
    pane.session.is_blocked_on_tty_prompt.return_value = tty_prompt
    return pane


class TestIdleWatchdog:
    def test_idle_streak_starts_when_pane_is_idle(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pane = _make_pane(state="working", at_ready_prompt=True)
        orch.panes["backend"] = pane

        monkeypatch.setattr(orch_mod.time, "time", lambda: 1000.0)
        orch._check_idle_teammates()

        assert orch._idle_state[_key("backend")]["first_idle_ts"] == 1000.0
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
        assert orch._idle_state[_key("backend")]["last_reminder_ts"] == clock[0]

    def test_processing_pane_resets_streak(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pane = _make_pane(state="working", at_ready_prompt=True)
        orch.panes["backend"] = pane

        clock = [1000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])

        orch._check_idle_teammates()
        assert orch._idle_state[_key("backend")]["first_idle_ts"] == 1000.0

        # Pane goes back to processing (long shell command) — streak resets.
        pane.session.is_at_ready_prompt.return_value = False
        clock[0] += 10
        orch._check_idle_teammates()
        assert orch._idle_state[_key("backend")]["first_idle_ts"] is None

        # When it comes back to idle, the streak restarts from scratch.
        pane.session.is_at_ready_prompt.return_value = True
        clock[0] += 5
        orch._check_idle_teammates()
        assert orch._idle_state[_key("backend")]["first_idle_ts"] == clock[0]

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
        assert _key("lead") not in orch._idle_state

    def test_non_working_state_clears_tracking(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pane = _make_pane(state="working", at_ready_prompt=True)
        orch.panes["backend"] = pane

        clock = [1000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])

        orch._check_idle_teammates()
        assert _key("backend") in orch._idle_state

        pane.state = "done"  # agent finally called `takkub done`
        orch._check_idle_teammates()
        assert _key("backend") not in orch._idle_state
        pane.session.write.assert_not_called()

    def test_dead_session_clears_tracking(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pane = _make_pane(state="working", at_ready_prompt=True, alive=False)
        orch.panes["backend"] = pane

        monkeypatch.setattr(orch_mod.time, "time", lambda: 1000.0)
        orch._check_idle_teammates()

        assert _key("backend") not in orch._idle_state
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
        orch._idle_state[_key("backend")] = {"first_idle_ts": 1.0, "last_reminder_ts": 2.0}

        # Suppress Qt timer-based auto-close inside done().
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", lambda *_args, **_kw: None)

        ok, _ = orch.done("backend", note="green")
        assert ok
        assert _key("backend") not in orch._idle_state

    def test_close_clears_idle_state(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pane = _make_pane(state="working", at_ready_prompt=True)
        orch.panes["backend"] = pane
        orch._idle_state[_key("backend")] = {"first_idle_ts": 1.0, "last_reminder_ts": 2.0}

        # paneClosed.emit happens in close(); the signal has no slots in tests
        # so it just no-ops. mark_expected_exit + terminate are mocks.
        orch.close("backend")
        assert _key("backend") not in orch._idle_state


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


class TestTtyBlockIdleWatchdog:
    """Issue #54: pane blocked on interactive subprocess prompt — idle watchdog
    behaviour and surface-notice logic."""

    def test_blocked_pane_does_not_fire_forgot_done_reminder(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(a) A pane stuck on a y/N prompt must NOT receive the 'forgot takkub done'
        idle reminder — that text is meaningless when a subprocess is blocking."""
        pane = _make_pane(state="working", at_ready_prompt=False, tty_prompt="Ok to proceed? (y)")
        orch.panes["backend"] = pane

        clock = [1000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", lambda *_a, **_kw: None)

        orch._check_idle_teammates()
        clock[0] += orch_mod.IDLE_REMIND_AFTER_S + 1
        orch._check_idle_teammates()

        # The idle reminder is written directly to the pane — it must NOT fire.
        for call in pane.session.write.call_args_list:
            assert IDLE_REMINDER_TEXT not in str(call), (
                "IDLE_REMINDER_TEXT must not be sent to a TTY-blocked pane"
            )

    def test_blocked_pane_surfaces_notice_to_lead_after_threshold(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(b) After TTY_BLOCK_SURFACE_AFTER_S of continuous blocking, the
        orchestrator injects a ⚠️ notice to Lead (not to the blocked pane)."""
        lead = _make_pane(state="active")
        orch.panes["lead"] = lead
        pane = _make_pane(state="working", at_ready_prompt=False, tty_prompt="[y/N]")
        orch.panes["backend"] = pane

        clock = [1000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", lambda *_a, **_kw: None)

        # Tick within threshold — no notice yet.
        orch._check_idle_teammates()
        assert lead.session.write.call_count == 0

        # Cross threshold — notice must fire.
        clock[0] += TTY_BLOCK_SURFACE_AFTER_S + 1
        orch._check_idle_teammates()
        assert lead.session.write.call_count == 1
        notice_text = str(lead.session.write.call_args_list[0])
        assert "[backend]" in notice_text
        assert "[y/N]" in notice_text

    def test_surface_notice_respects_cooldown(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(b) Surface notice must not spam — cooldown TTY_BLOCK_SURFACE_COOLDOWN_S."""
        lead = _make_pane(state="active")
        orch.panes["lead"] = lead
        pane = _make_pane(state="working", at_ready_prompt=False, tty_prompt="Enter passphrase:")
        orch.panes["backend"] = pane

        clock = [1000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", lambda *_a, **_kw: None)

        orch._check_idle_teammates()
        clock[0] += TTY_BLOCK_SURFACE_AFTER_S + 1
        orch._check_idle_teammates()  # first surface
        assert lead.session.write.call_count == 1

        # Still inside cooldown — should not re-surface.
        clock[0] += TTY_BLOCK_SURFACE_COOLDOWN_S - 1
        orch._check_idle_teammates()
        assert lead.session.write.call_count == 1

        # After cooldown — surface again.
        clock[0] += 2
        orch._check_idle_teammates()
        assert lead.session.write.call_count == 2

    def test_normal_idle_pane_behavior_unchanged(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(d) A pane that is NOT TTY-blocked still gets the normal idle reminder."""
        pane = _make_pane(state="working", at_ready_prompt=True, tty_prompt=None)
        orch.panes["backend"] = pane

        clock = [1000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", lambda *_a, **_kw: None)

        orch._check_idle_teammates()
        clock[0] += orch_mod.IDLE_REMIND_AFTER_S + 1
        orch._check_idle_teammates()

        pane.session.write.assert_called_with(IDLE_REMINDER_TEXT)
