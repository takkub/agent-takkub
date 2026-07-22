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
    MALFORMED_XML_NOTICE_COOLDOWN_S,
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
    unparsed_xml: str | None = None,
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
    # Default: no malformed tool-call XML on screen.
    pane.session.has_unparsed_tool_call.return_value = unparsed_xml
    # Default: input box is empty (real PtySession returns False when no
    # "[Pasted text +N lines]" placeholder is showing). Without this a
    # MagicMock would return a truthy stub and the stuck-paste reaper would
    # fire a recovery CR in every test.
    pane.session.shows_pending_input.return_value = False
    # Default: not in a provider MCP-boot / queued-message phase. Without this a
    # MagicMock would return a truthy stub and the boot/queue suppression gate
    # would treat every idle pane as "still booting".
    pane.session.shows_startup_marker.return_value = False
    return pane


def _work_then_idle(orch: Orchestrator, pane: MagicMock, clock: list) -> None:
    """Drive one genuine work turn (busy, non-startup) then back to idle, so the
    watchdog's `seen_working` latch arms — mirrors a real task starting to run
    before the pane goes idle. The forgot-`takkub done` reminder only fires
    after this (a pane that never actually ran its task isn't "forgot done")."""
    pane.session.is_at_ready_prompt.return_value = False
    pane.session.shows_startup_marker.return_value = False
    orch._check_idle_teammates()
    pane.session.is_at_ready_prompt.return_value = True
    clock[0] += 1
    orch._check_idle_teammates()


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

        _work_then_idle(orch, pane, clock)  # a real turn runs, then idle
        orch._check_idle_teammates()  # start the idle streak
        clock[0] += orch_mod.IDLE_REMIND_AFTER_S + 1  # cross threshold
        orch._check_idle_teammates()

        pane.session.write.assert_called_with(orch_mod.IDLE_REMINDER_TEXT)
        assert orch._idle_state[_key("backend")]["last_reminder_ts"] == clock[0]

    def test_booting_pane_never_reminded(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # codex/agy cold-boot: the composer status bar can read idle ("Fast
        # off") while MCP servers boot and a delivered task sits queued. The
        # watchdog must NOT count that as forgot-`takkub done` (2026-07-21
        # boot-window reminder-stacking bug).
        pane = _make_pane(state="working", at_ready_prompt=True)
        pane.session.shows_startup_marker.return_value = True
        orch.panes["codex"] = pane

        clock = [1000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])

        orch._check_idle_teammates()
        clock[0] += orch_mod.IDLE_REMIND_AFTER_S + orch_mod.IDLE_REMIND_COOLDOWN_S + 10
        orch._check_idle_teammates()

        pane.session.write.assert_not_called()
        assert orch._idle_state[_key("codex")]["first_idle_ts"] is None

    def test_fast_task_still_reminded_via_unlatched_fallback(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A task that starts AND finishes between two watchdog ticks never lets
        # the tick observe a busy state, so `seen_working` stays False. Such a
        # pane really did finish, so it must still be reminded once it has been
        # idle past any provider boot/queue window — otherwise the boot fix
        # would strand genuinely-forgot-done panes forever.
        pane = _make_pane(state="working", at_ready_prompt=True)
        orch.panes["backend"] = pane

        clock = [1000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])

        orch._check_idle_teammates()  # start the streak; never seen busy
        assert orch._idle_state[_key("backend")]["seen_working"] is False

        # Past IDLE_REMIND_AFTER_S alone: still suppressed (could be booting).
        clock[0] += orch_mod.IDLE_REMIND_AFTER_S + 1
        orch._check_idle_teammates()
        pane.session.write.assert_not_called()

        # Past the unlatched fallback: the pane is certainly not booting.
        clock[0] += orch_mod.IDLE_REMIND_UNLATCHED_AFTER_S
        orch._check_idle_teammates()
        pane.session.write.assert_called_with(orch_mod.IDLE_REMINDER_TEXT)

    def test_booting_pane_not_reminded_even_past_unlatched_fallback(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The fallback must not defeat the boot fix: while the startup/queue
        # marker is on screen the idle streak keeps resetting, so idle_for
        # never reaches the fallback threshold.
        pane = _make_pane(state="working", at_ready_prompt=True)
        pane.session.shows_startup_marker.return_value = True
        orch.panes["codex"] = pane

        clock = [1000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])

        orch._check_idle_teammates()
        clock[0] += orch_mod.IDLE_REMIND_UNLATCHED_AFTER_S * 2
        orch._check_idle_teammates()

        pane.session.write.assert_not_called()

    def test_pane_that_never_ran_is_not_reminded_before_the_fallback(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A pane that received its task but never actually ran a work turn
        # (queued the whole time) has seen_working=False, so no reminder — a
        # task can't be "finished but forgot done" without ever having run.
        pane = _make_pane(state="working", at_ready_prompt=True)
        orch.panes["backend"] = pane

        clock = [1000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])

        orch._check_idle_teammates()
        clock[0] += orch_mod.IDLE_REMIND_AFTER_S + 1
        orch._check_idle_teammates()

        pane.session.write.assert_not_called()

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

        # First reminder fires (after a real work turn arms the latch).
        _work_then_idle(orch, pane, clock)
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


class TestStuckPasteReaper:
    """The reaper that submits a task paste whose Enter was swallowed.

    Regression for the 2026-07-02 QA fan-out incident: parallel spawn swallowed
    the submitting Enter, the delivery self-heal exhausted within ~3 s, and a
    false rate-limit flag (Fable-5 promo text) suppressed the idle reminder
    whose trailing Enter used to rescue the pane by accident — leaving
    "[Pasted text +N lines]" stuck in the input box for hours.
    """

    def _stuck_pane(self) -> MagicMock:
        pane = _make_pane(state="working", at_ready_prompt=True)
        pane.session.shows_pending_input.return_value = True
        return pane

    def test_submits_after_threshold(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pane = self._stuck_pane()
        orch.panes["qa#3"] = pane

        clock = [1000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])

        orch._check_idle_teammates()  # first observation — starts the episode
        pane.session.write.assert_not_called()

        clock[0] += orch_mod.STUCK_PASTE_SUBMIT_AFTER_S + 1
        orch._check_idle_teammates()
        pane.session.write.assert_called_once_with(b"\r")
        assert orch._ps(_key("qa#3")).pending_submit_attempts == 1

    def test_fires_even_when_rate_limit_flag_suppresses_reminder(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The exact incident shape: pane falsely flagged rate-limited for 5 h.
        # The idle reminder must stay suppressed, but the reaper must still
        # submit the stuck paste.
        pane = self._stuck_pane()
        orch.panes["qa#3"] = pane

        clock = [1000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])
        orch._ps(_key("qa#3")).rate_limited_until = clock[0] + 18000

        orch._check_idle_teammates()
        clock[0] += orch_mod.IDLE_REMIND_AFTER_S + 1  # past the reminder threshold too
        orch._check_idle_teammates()

        pane.session.write.assert_called_once_with(b"\r")  # reaper fired…
        assert IDLE_REMINDER_TEXT not in [
            c.args[0] for c in pane.session.write.call_args_list
        ]  # …but the reminder stayed suppressed

    def test_episode_resets_when_input_clears(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pane = self._stuck_pane()
        orch.panes["backend"] = pane

        clock = [1000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])

        orch._check_idle_teammates()  # episode starts
        pane.session.shows_pending_input.return_value = False  # submit landed
        clock[0] += orch_mod.STUCK_PASTE_SUBMIT_AFTER_S + 1
        orch._check_idle_teammates()

        pane.session.write.assert_not_called()
        ps = orch._ps(_key("backend"))
        assert ps.pending_input_since is None
        assert ps.pending_submit_attempts == 0

    def test_cooldown_and_attempt_cap(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pane = self._stuck_pane()
        orch.panes["backend"] = pane

        clock = [1000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])
        events: list[str] = []
        monkeypatch.setattr(orch_mod, "_log_event", lambda event, **kw: events.append(event))

        orch._check_idle_teammates()  # episode starts
        clock[0] += orch_mod.STUCK_PASTE_SUBMIT_AFTER_S + 1
        orch._check_idle_teammates()  # attempt 1
        orch._check_idle_teammates()  # inside cooldown — no attempt
        cr_writes = [c for c in pane.session.write.call_args_list if c.args[0] == b"\r"]
        assert len(cr_writes) == 1

        # Walk the clock through every remaining attempt, then well past it.
        for _ in range(orch_mod.STUCK_PASTE_SUBMIT_MAX + 2):
            clock[0] += orch_mod.STUCK_PASTE_SUBMIT_COOLDOWN_S + 1
            orch._check_idle_teammates()

        cr_writes = [c for c in pane.session.write.call_args_list if c.args[0] == b"\r"]
        assert len(cr_writes) == orch_mod.STUCK_PASTE_SUBMIT_MAX  # capped
        assert "stuck_paste_gave_up" in events


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

        _work_then_idle(orch, pane, clock)
        orch._check_idle_teammates()
        clock[0] += orch_mod.IDLE_REMIND_AFTER_S + 1
        orch._check_idle_teammates()

        pane.session.write.assert_called_with(IDLE_REMINDER_TEXT)


class TestMalformedXmlWatchdog:
    """Issue #59: pane has literal tool-call XML on screen (harness silently
    no-op'd it due to missing namespace prefix) -- watchdog must inject a nudge."""

    def test_teammate_idle_with_xml_gets_nudge(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A teammate pane that is idle AND shows malformed XML gets a cockpit nudge."""
        pane = _make_pane(
            state="working",
            at_ready_prompt=True,
            unparsed_xml='<invoke name="Bash">',
        )
        orch.panes["backend"] = pane

        clock = [1000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", lambda *_a, **_kw: None)

        orch._check_idle_teammates()

        # The nudge must be written to the affected pane.
        assert pane.session.write.call_count >= 1
        notice_text = str(pane.session.write.call_args_list[0])
        assert "cockpit" in notice_text
        assert "antml" in notice_text

    def test_lead_idle_with_xml_gets_nudge(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lead pane (normally exempt from idle-reminder loop) still gets the
        malformed-XML nudge because it's the role most likely to emit tool calls."""
        lead = _make_pane(
            state="active",
            at_ready_prompt=True,
            unparsed_xml='<invoke name="Read">',
        )
        orch.panes["lead"] = lead

        clock = [1000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", lambda *_a, **_kw: None)

        orch._check_idle_teammates()

        assert lead.session.write.call_count >= 1
        notice_text = str(lead.session.write.call_args_list[0])
        assert "cockpit" in notice_text

    def test_nudge_not_sent_when_no_xml(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pane with normal screen content must NOT receive the XML nudge."""
        pane = _make_pane(state="working", at_ready_prompt=True, unparsed_xml=None)
        orch.panes["backend"] = pane

        clock = [1000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", lambda *_a, **_kw: None)

        orch._check_idle_teammates()

        # No write at all on first tick (idle streak not yet expired either).
        pane.session.write.assert_not_called()

    def test_nudge_cooldown_prevents_spam(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The XML nudge must not repeat more often than MALFORMED_XML_NOTICE_COOLDOWN_S.

        (The idle-done reminder is a separate write path and may also fire in
        this window; we track only the XML-nudge message specifically.)
        """
        pane = _make_pane(
            state="working",
            at_ready_prompt=True,
            unparsed_xml="<function_calls>",
        )
        orch.panes["backend"] = pane

        clock = [1000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", lambda *_a, **_kw: None)

        def _xml_nudge_count() -> int:
            return sum(
                1
                for call in pane.session.write.call_args_list
                if "cockpit" in str(call) and "antml" in str(call)
            )

        # First tick -- XML nudge fires once.
        orch._check_idle_teammates()
        assert _xml_nudge_count() == 1

        # Inside cooldown -- XML nudge must NOT repeat.
        clock[0] += MALFORMED_XML_NOTICE_COOLDOWN_S - 1
        orch._check_idle_teammates()
        assert _xml_nudge_count() == 1

        # After cooldown elapses -- XML nudge fires again.
        clock[0] += 2
        orch._check_idle_teammates()
        assert _xml_nudge_count() == 2

    def test_lead_not_busy_no_nudge(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lead pane that is NOT at its ready prompt (busy) must NOT get nudged."""
        lead = _make_pane(
            state="active",
            at_ready_prompt=False,
            unparsed_xml='<invoke name="Bash">',
        )
        orch.panes["lead"] = lead

        clock = [1000.0]
        monkeypatch.setattr(orch_mod.time, "time", lambda: clock[0])
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", lambda *_a, **_kw: None)

        orch._check_idle_teammates()

        lead.session.write.assert_not_called()


class TestWatchdogExceptionLogging:
    """The per-pane watchdog body's catch-all used to log a bare role/project
    with NO exception detail, re-firing every 5s tick (3279 blind entries in one
    events.log). It now captures the exception type+message and dedups per pane."""

    def test_exception_logged_with_detail_and_deduped(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pane = _make_pane(state="working", at_ready_prompt=True)
        # Raise from inside the watchdog body (the tty-prompt probe runs before
        # the ready check) so the catch-all fires.
        pane.session.is_blocked_on_tty_prompt.side_effect = RuntimeError("boom")
        orch.panes["backend"] = pane

        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(orch_mod, "_log_event", lambda ev, **kw: events.append((ev, kw)))
        monkeypatch.setattr(orch_mod.time, "time", lambda: 1000.0)

        orch._check_idle_teammates()
        orch._check_idle_teammates()  # same fault, same tick window → deduped

        errs = [kw for ev, kw in events if ev == "idle_watchdog_pane_error"]
        assert len(errs) == 1, "persistent fault must log once, not per-tick"
        assert "RuntimeError: boom" in errs[0]["err"]

    def test_changed_error_logs_again(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pane = _make_pane(state="working", at_ready_prompt=True)
        orch.panes["backend"] = pane
        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(orch_mod, "_log_event", lambda ev, **kw: events.append((ev, kw)))
        monkeypatch.setattr(orch_mod.time, "time", lambda: 1000.0)

        pane.session.is_blocked_on_tty_prompt.side_effect = RuntimeError("first")
        orch._check_idle_teammates()
        pane.session.is_blocked_on_tty_prompt.side_effect = ValueError("second")
        orch._check_idle_teammates()  # different error string → logs again

        errs = [kw["err"] for ev, kw in events if ev == "idle_watchdog_pane_error"]
        assert any("RuntimeError: first" in e for e in errs)
        assert any("ValueError: second" in e for e in errs)
