"""Tests for #520: the stuck-pane watchdog must not mistake a machine
sleep/suspend gap for pane inactivity.

`_check_idle_teammates` rides a 5s QTimer. A QTimer cannot fire while the
process is suspended, so the wall-clock gap between two consecutive ticks
is itself proof the machine was asleep (as opposed to normal jitter). These
tests drive `_absorb_watchdog_sleep_gap` + `_check_stuck_panes` directly
against a hand-built stub instance, the same style test_stuck_recover.py
uses, so no PyQt6 event loop or live PtySession is needed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agent_takkub.orchestrator import (
    STUCK_THRESHOLD_S,
    WATCHDOG_SLEEP_GAP_THRESHOLD_S,
    Orchestrator,
    PaneState,
)


class _FakePane:
    def __init__(self, state: str = "working", last_out: float = 0.0) -> None:
        self.state = state
        self._last_output_ts = last_out
        self._session_cwd = "/x"
        sess = MagicMock()
        sess.is_alive = True
        sess.is_blocked_on_tty_prompt.return_value = None
        sess.is_blocked_on_permission_prompt.return_value = None
        sess.is_at_update_splash.return_value = False
        self.session = sess


class _FakeOrch:
    def __init__(self) -> None:
        self._panes_by_project: dict = {}
        self._pane_state: dict = {}
        self._idle_state: dict = {}
        self._recent_exits: dict = {}
        self._watchdog_last_tick_wall_ts: float = 0.0
        self.close_calls: list[tuple[str, str]] = []
        self.spawn_calls: list[tuple[str, str | None, str]] = []
        self.notify_calls: list[tuple[str, str, str | None]] = []

    def _ps(self, key: str) -> PaneState:
        try:
            return self._pane_state[key]
        except KeyError:
            ps = PaneState()
            self._pane_state[key] = ps
            return ps

    def close(self, role, project=None, suppress_pipeline=False, suppress_auto_chain=False, **_kw):
        self.close_calls.append((role, project or ""))
        return True, "ok"

    def spawn(self, role, cwd=None, project=None, **_kw):
        self.spawn_calls.append((role, cwd, project or ""))
        return True, "ok"

    def _send_when_ready(self, role, task, project=None) -> None:
        pass

    def _auto_recover_stuck(self, role, project, pane, now) -> None:
        Orchestrator._auto_recover_stuck(self, role, project, pane, now)  # type: ignore[arg-type]

    def _check_shell_open_dialog(self, project_name, role, pane, key, now) -> None:
        Orchestrator._check_shell_open_dialog(self, project_name, role, pane, key, now)  # type: ignore[arg-type]

    def _notify_lead(self, project, notice, from_role=None, note="", **_kwargs) -> None:
        self.notify_calls.append((project, notice, from_role))

    def _live_non_scaffolding_children(self, project_ns, role_name, session) -> list[str]:
        return []

    def _defer_stuck_recover_for_live_children(self, role, project, pane, ps_ck, now) -> bool:
        return Orchestrator._defer_stuck_recover_for_live_children(  # type: ignore[arg-type]
            self, role, project, pane, ps_ck, now
        )


def _absorb(fake: _FakeOrch, now: float) -> None:
    Orchestrator._absorb_watchdog_sleep_gap(fake, now)  # type: ignore[arg-type]


def _check(fake: _FakeOrch, now: float) -> None:
    Orchestrator._check_stuck_panes(fake, now)  # type: ignore[arg-type]


def _tick(fake: _FakeOrch, now: float) -> None:
    """Mirrors the real _check_idle_teammates call order for #520."""
    _absorb(fake, now)
    _check(fake, now)


class TestWatchdogSleepGapAbsorption:
    def test_no_prior_tick_is_a_noop(self) -> None:
        # First-ever tick: nothing to compare against, must not explode or
        # touch any pane state.
        fake = _FakeOrch()
        pane = _FakePane(state="working", last_out=1000.0)
        fake._panes_by_project["p"] = {"backend": pane}
        fake._ps("p::backend").last_content_change_ts = 1000.0
        _absorb(fake, 1000.0)
        assert fake._pane_state["p::backend"].last_content_change_ts == 1000.0
        assert pane._last_output_ts == 1000.0
        assert fake._watchdog_last_tick_wall_ts == 1000.0

    def test_normal_cadence_does_not_shift_clocks(self) -> None:
        # Ticks arriving on the normal 5s cadence (gap well under the sleep
        # threshold) must never bump the content clock — a genuinely stuck
        # pane still has to trip STUCK_THRESHOLD_S for real.
        fake = _FakeOrch()
        pane = _FakePane(state="working", last_out=1000.0)
        fake._panes_by_project["p"] = {"backend": pane}
        fake._ps("p::backend").last_content_change_ts = 1000.0

        now = 1000.0
        for _ in range(20):
            now += 5.0
            _absorb(fake, now)

        assert fake._pane_state["p::backend"].last_content_change_ts == 1000.0
        assert pane._last_output_ts == 1000.0

    def test_genuine_stuck_pane_without_sleep_gap_still_recovers(self) -> None:
        # Regression guard: normal-cadence ticks across a real
        # STUCK_THRESHOLD_S-long wedge must still recover the pane.
        fake = _FakeOrch()
        pane = _FakePane(state="working", last_out=1000.0)
        fake._panes_by_project["agent-takkub"] = {"backend": pane}
        fake._ps("agent-takkub::backend").last_content_change_ts = 1000.0

        now = 1000.0
        end = 1000.0 + STUCK_THRESHOLD_S + 1
        while now < end:
            now += 5.0
            _tick(fake, now)

        assert fake.close_calls == [("backend", "agent-takkub")]

    def test_sleep_gap_prevents_false_positive_recover(self) -> None:
        # The reported #520 scenario: the machine sleeps for longer than
        # STUCK_THRESHOLD_S. Only two ticks are ever observed — one right
        # before sleep, one right after wake — with nothing in between.
        fake = _FakeOrch()
        pane = _FakePane(state="working", last_out=1000.0)
        fake._panes_by_project["agent-takkub"] = {"backend": pane}

        _tick(fake, 1000.0)  # establishes last_content_change_ts = 1000.0
        assert fake._pane_state["agent-takkub::backend"].last_content_change_ts == 1000.0

        sleep_gap = STUCK_THRESHOLD_S + 300  # well past the stuck threshold
        woke_at = 1000.0 + sleep_gap
        _tick(fake, woke_at)

        assert fake.close_calls == [], (
            "sleeping through STUCK_THRESHOLD_S must not trigger an auto-respawn"
        )
        assert fake.spawn_calls == []

    def test_sleep_gap_logs_the_absorbed_duration(self, monkeypatch) -> None:
        import agent_takkub.orchestrator as orch_mod

        logged: list[dict] = []
        monkeypatch.setattr(
            orch_mod, "_log_event", lambda event, **kw: logged.append({"event": event, **kw})
        )

        fake = _FakeOrch()
        _absorb(fake, 1000.0)
        gap = WATCHDOG_SLEEP_GAP_THRESHOLD_S + 60
        _absorb(fake, 1000.0 + gap)

        rec = next((e for e in logged if e["event"] == "watchdog_clock_sleep_gap_absorbed"), None)
        assert rec is not None
        assert rec["gap_s"] == int(gap)

    def test_gap_below_threshold_is_not_absorbed(self) -> None:
        # A single slow tick (e.g. GC pause) just under the sleep threshold
        # must not be treated as a sleep — it's real elapsed time.
        fake = _FakeOrch()
        pane = _FakePane(state="working", last_out=1000.0)
        fake._panes_by_project["p"] = {"backend": pane}
        fake._ps("p::backend").last_content_change_ts = 1000.0

        _absorb(fake, 1000.0)
        _absorb(fake, 1000.0 + WATCHDOG_SLEEP_GAP_THRESHOLD_S - 1)

        assert fake._pane_state["p::backend"].last_content_change_ts == 1000.0
        assert pane._last_output_ts == 1000.0

    def test_unset_content_clock_is_left_none(self) -> None:
        # A pane with no content-change clock yet (fresh PaneState) must not
        # have None turned into a number by the absorber.
        fake = _FakeOrch()
        fake._ps("p::backend")  # create with default last_content_change_ts=None
        _absorb(fake, 1000.0)
        _absorb(fake, 1000.0 + WATCHDOG_SLEEP_GAP_THRESHOLD_S + 60)
        assert fake._pane_state["p::backend"].last_content_change_ts is None
