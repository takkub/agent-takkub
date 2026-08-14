"""Tests for `_check_stuck_panes`, the watchdog that auto-recovers
teammate panes wedged with no PTY output. Drives the unbound method
against a hand-built stub instance so we don't need PyQt6 + a live
PtySession to exercise the threshold + cooldown logic.

What the tests pin down (the contract the auto-recover relies on):
  - Lead is exempt; teammates only.
  - State must be `working` AND session alive.
  - Output silence < threshold → no-op.
  - Output silence >= threshold → close + scheduled spawn.
  - A recover inside cooldown is suppressed so we don't loop.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent_takkub.orchestrator import (
    LEAD,
    STUCK_RECOVER_COOLDOWN_S,
    STUCK_RECOVER_MAX,
    STUCK_THRESHOLD_S,
    TTY_BLOCK_SURFACE_COOLDOWN_S,
    Orchestrator,
    PaneState,
    PipelineRun,
)


class _FakePane:
    """Minimal AgentPane stand-in. The watchdog only reads the fields
    listed here; making them attributes (not MagicMock) keeps the
    isinstance(float) guard happy without per-test setup boilerplate."""

    def __init__(
        self,
        state: str = "working",
        last_out: float = 0.0,
        session_alive: bool = True,
        cwd: str = "/x",
    ) -> None:
        self.state = state
        self._last_output_ts = last_out
        self._session_cwd = cwd
        if session_alive:
            sess = MagicMock()
            sess.is_alive = True
            # Default: not blocked on a TTY prompt. Without this, MagicMock
            # returns a truthy stub and the TTY-block gate defers every recovery.
            sess.is_blocked_on_tty_prompt.return_value = None
            # Default: no update splash (#62). MagicMock returns truthy otherwise,
            # which would route every recovery through the splash path.
            sess.is_at_update_splash.return_value = False
            self.session = sess
        else:
            self.session = None


class _FakeOrch:
    """Carries just the orchestrator state `_check_stuck_panes` reads
    and the calls it makes. close/QTimer.singleShot are recorded so
    tests can assert the recover path fired."""

    def __init__(self) -> None:
        self._panes_by_project = {}
        self._pane_state = {}  # key -> PaneState; NOT popped by close() so cooldown survives
        self._idle_state = {}
        self._recent_exits = {}
        self.close_calls: list[tuple[str, str]] = []
        self.spawn_calls: list[tuple[str, str | None, str]] = []
        self.tty_surface_calls: list[tuple[str, str, str]] = []
        self.notify_calls: list[tuple[str, str, str | None]] = []

    def _ps(self, key: str) -> PaneState:
        try:
            return self._pane_state[key]
        except KeyError:
            ps = PaneState()
            self._pane_state[key] = ps
            return ps

    def close(
        self,
        role: str,
        project: str | None = None,
        suppress_pipeline: bool = False,
        suppress_auto_chain: bool = False,
        **_kw,
    ) -> tuple[bool, str]:
        # Mimic the orchestrator's close() clearing the snapshot/restore fields
        # (session_uuid, task, auto_chain, requires_commit).
        # Intentionally preserve last_stuck_recover so cooldown tests work
        # (real close() pops it, but the fake is a minimal stub for snapshot tests).
        key = f"{project or ''}::{role}"
        ps = self._pane_state.get(key)
        if ps is not None:
            ps.session_uuid = None
            ps.session_uuid_cwd = ""
            ps.last_assigned_task = None
            ps.auto_chain = False
            ps.requires_commit_on_done = False
        self._idle_state.pop(key, None)
        self.close_calls.append((role, project or ""))
        return True, "ok"

    def spawn(self, role: str, cwd: str | None = None, project: str | None = None, **_kw):
        self.spawn_calls.append((role, cwd, project or ""))
        return True, "ok"

    def _send_when_ready(self, role: str, task: str, project: str | None = None) -> None:
        pass  # no-op in tests

    def _auto_recover_stuck(self, role, project, pane, now) -> None:
        # Delegate to the real orchestrator method so the cooldown
        # bookkeeping + output-ts reset run for real. Tests that drive
        # `_check_stuck_panes` rely on this method being reachable on
        # the fake (the watchdog calls `self._auto_recover_stuck`).
        Orchestrator._auto_recover_stuck(self, role, project, pane, now)  # type: ignore[arg-type]

    def _maybe_surface_tty_block(self, key, role, project, prompt_line, now) -> None:
        Orchestrator._maybe_surface_tty_block(self, key, role, project, prompt_line, now)  # type: ignore[arg-type]

    def _surface_tty_block_notice(self, role, project, prompt_line) -> None:
        # Record that Lead was notified so tests can assert surface behaviour.
        self.tty_surface_calls.append((role, project, prompt_line))

    def _check_shell_open_dialog(self, project_name, role, pane, key, now) -> None:
        # Delegate to the real method (#104) — driven for real so its
        # transcript-tail read + dedupe + _notify_lead call are exercised.
        Orchestrator._check_shell_open_dialog(self, project_name, role, pane, key, now)  # type: ignore[arg-type]

    def _notify_lead(self, project, notice, from_role=None, note="") -> None:
        self.notify_calls.append((project, notice, from_role))


@pytest.fixture(autouse=True)
def _patch_qtimer(monkeypatch: pytest.MonkeyPatch) -> list:
    """Capture QTimer.singleShot invocations and fire them inline so
    the spawn call happens during the test rather than on a real Qt
    event loop tick."""
    fired: list[tuple[int, object]] = []

    class _ShotCapture:
        @staticmethod
        def singleShot(ms, fn):
            fired.append((ms, fn))
            fn()  # run immediately so test assertions see the spawn

    monkeypatch.setattr("agent_takkub.orchestrator.QTimer", _ShotCapture)
    return fired


class _SyncThread:
    """Runs target() inline instead of on a real thread (issue #194's
    _check_shell_open_dialog background scan) so tests stay deterministic
    instead of racing a real thread against the assertions right after."""

    def __init__(self, target=None, args=(), kwargs=None, name=None, daemon=None) -> None:
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self) -> None:
        self._target(*self._args, **self._kwargs)


@pytest.fixture(autouse=True)
def _patch_dialog_scan_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_takkub.orchestrator.threading.Thread", _SyncThread)


def _check(fake: _FakeOrch, now: float) -> None:
    Orchestrator._check_stuck_panes(fake, now)  # type: ignore[arg-type]


def _recover(fake: _FakeOrch, role: str, project: str, pane, now: float) -> None:
    Orchestrator._auto_recover_stuck(fake, role, project, pane, now)  # type: ignore[arg-type]


class TestCheckStuckPanes:
    def test_lead_is_exempt(self) -> None:
        fake = _FakeOrch()
        now = 1_000_000.0
        fake._panes_by_project["p"] = {
            LEAD.name: _FakePane(state="working", last_out=now - STUCK_THRESHOLD_S - 10)
        }
        _check(fake, now)
        assert fake.close_calls == []
        assert fake.spawn_calls == []

    def test_non_working_state_skipped(self) -> None:
        fake = _FakeOrch()
        now = 1_000_000.0
        # state "done" — agent already reported, no need to recover
        fake._panes_by_project["p"] = {
            "backend": _FakePane(state="done", last_out=now - STUCK_THRESHOLD_S - 10)
        }
        _check(fake, now)
        assert fake.close_calls == []

    def test_dead_session_skipped(self) -> None:
        fake = _FakeOrch()
        now = 1_000_000.0
        fake._panes_by_project["p"] = {
            "backend": _FakePane(
                state="working",
                last_out=now - STUCK_THRESHOLD_S - 10,
                session_alive=False,
            )
        }
        _check(fake, now)
        assert fake.close_calls == []

    def test_uninitialised_timestamp_skipped(self) -> None:
        # Pane just spawned, no bytes yet — last_out = 0.0. Must not
        # trigger because 0 < STUCK_THRESHOLD_S is unsatisfiable here
        # (`(now - 0) > threshold` would be a false positive).
        fake = _FakeOrch()
        now = 1_000_000.0
        fake._panes_by_project["p"] = {"backend": _FakePane(state="working", last_out=0.0)}
        _check(fake, now)
        assert fake.close_calls == []

    def test_silence_under_threshold_skipped(self) -> None:
        fake = _FakeOrch()
        now = 1_000_000.0
        fake._panes_by_project["p"] = {
            "backend": _FakePane(state="working", last_out=now - (STUCK_THRESHOLD_S - 30))
        }
        _check(fake, now)
        assert fake.close_calls == []

    def test_silence_over_threshold_triggers_recover(self) -> None:
        fake = _FakeOrch()
        now = 1_000_000.0
        fake._panes_by_project["agent-takkub"] = {
            "backend": _FakePane(
                state="working",
                last_out=now - STUCK_THRESHOLD_S - 1,
                cwd="C:/foo",
            )
        }
        _check(fake, now)
        assert fake.close_calls == [("backend", "agent-takkub")]
        assert fake.spawn_calls == [("backend", "C:/foo", "agent-takkub")]
        # Cooldown stamp set so a subsequent tick doesn't loop.
        assert (
            fake._pane_state.get("agent-takkub::backend") or PaneState()
        ).last_stuck_recover == now

    def test_cooldown_suppresses_back_to_back_recover(self) -> None:
        # Same pane stuck twice within the cooldown window — second
        # check must NOT re-trigger close/spawn.
        fake = _FakeOrch()
        now = 1_000_000.0
        fake._panes_by_project["p"] = {
            "backend": _FakePane(state="working", last_out=now - STUCK_THRESHOLD_S - 1)
        }
        _check(fake, now)
        assert len(fake.close_calls) == 1
        # Advance just under cooldown
        now2 = now + STUCK_RECOVER_COOLDOWN_S - 1
        fake._panes_by_project["p"]["backend"]._last_output_ts = now2 - STUCK_THRESHOLD_S - 1
        _check(fake, now2)
        # No new close — still in cooldown
        assert len(fake.close_calls) == 1

    def test_cooldown_expires_allows_second_recover(self) -> None:
        fake = _FakeOrch()
        now = 1_000_000.0
        pane = _FakePane(state="working", last_out=now - STUCK_THRESHOLD_S - 1)
        fake._panes_by_project["p"] = {"backend": pane}
        _check(fake, now)
        # Advance past cooldown
        now2 = now + STUCK_RECOVER_COOLDOWN_S + 1
        pane._last_output_ts = now2 - STUCK_THRESHOLD_S - 1
        _check(fake, now2)
        assert len(fake.close_calls) == 2


class TestAutoRecoverStuck:
    def test_resets_output_ts_to_now(self) -> None:
        # After recovering, _last_output_ts must be bumped so the next
        # 5 s tick doesn't immediately trigger another recover before
        # the respawned claude prints anything.
        fake = _FakeOrch()
        now = 1_000_000.0
        pane = _FakePane(state="working", last_out=now - STUCK_THRESHOLD_S - 1)
        fake._panes_by_project["p"] = {"backend": pane}
        _recover(fake, "backend", "p", pane, now)
        assert pane._last_output_ts == now

    def test_stamps_cooldown(self) -> None:
        fake = _FakeOrch()
        now = 1_000_000.0
        pane = _FakePane(state="working", last_out=now - STUCK_THRESHOLD_S - 1)
        _recover(fake, "backend", "p", pane, now)
        assert (fake._pane_state.get("p::backend") or PaneState()).last_stuck_recover == now

    def test_cooldown_survives_real_close_pop(self) -> None:
        """Regression: last_stuck_recover must survive close()'s _pane_state.pop().

        The default _FakeOrch.close() preserves _pane_state to keep snapshot
        tests simple — but that divergence masks the real bug. This test uses a
        pop-on-close variant that mirrors the production lifecycle: close() pops
        the whole PaneState, then _do_respawn() must restore last_stuck_recover
        so the watchdog can't re-trigger before STUCK_RECOVER_COOLDOWN_S expires.
        """

        class _PopOnClose(_FakeOrch):
            def close(
                self,
                role: str,
                project: str | None = None,
                suppress_pipeline: bool = False,
                suppress_auto_chain: bool = False,
                **_kw,
            ) -> tuple[bool, str]:
                key = f"{project or ''}::{role}"
                self._pane_state.pop(key, None)
                self._idle_state.pop(key, None)
                self.close_calls.append((role, project or ""))
                return True, "ok"

        fake = _PopOnClose()
        now = 1_000_000.0
        pane = _FakePane(state="working", last_out=now - STUCK_THRESHOLD_S - 1)
        fake._panes_by_project["p"] = {"backend": pane}
        _recover(fake, "backend", "p", pane, now)

        ps = fake._pane_state.get("p::backend")
        assert ps is not None, "PaneState must exist after respawn"
        assert ps.last_stuck_recover == now, (
            "cooldown stamp lost across close()->pop()->_do_respawn(); "
            "watchdog will re-trigger without honoring STUCK_RECOVER_COOLDOWN_S"
        )

    def test_cooldown_check_suppressed_after_real_close_pop(self) -> None:
        """Full path with real pop: _check_stuck_panes → recover → second check
        must be suppressed by the cooldown stamp restored in _do_respawn."""

        class _PopOnClose(_FakeOrch):
            def close(
                self,
                role: str,
                project: str | None = None,
                suppress_pipeline: bool = False,
                suppress_auto_chain: bool = False,
                **_kw,
            ) -> tuple[bool, str]:
                key = f"{project or ''}::{role}"
                self._pane_state.pop(key, None)
                self._idle_state.pop(key, None)
                self.close_calls.append((role, project or ""))
                return True, "ok"

        fake = _PopOnClose()
        now = 1_000_000.0
        pane = _FakePane(state="working", last_out=now - STUCK_THRESHOLD_S - 1)
        fake._panes_by_project["p"] = {"backend": pane}
        _check(fake, now)
        assert len(fake.close_calls) == 1

        now2 = now + STUCK_RECOVER_COOLDOWN_S - 1
        pane._last_output_ts = now2 - STUCK_THRESHOLD_S - 1
        _check(fake, now2)
        assert len(fake.close_calls) == 1, (
            "_do_respawn did not restore last_stuck_recover after real close()->pop(); "
            "second check fired within cooldown window"
        )

    def test_silent_for_s_computed_before_timestamp_reset(self) -> None:
        """Regression: silent_for_s must capture the *pre-reset* duration,
        not 0 (which is what you get if you reset _last_output_ts first)."""

        # We verify the logged value by monkeypatching _log_event to capture kwargs.
        logged: list[dict] = []

        import agent_takkub.orchestrator as orch_mod

        orig_log = orch_mod._log_event

        def capture_log(event, **kw):
            logged.append({"event": event, **kw})

        orch_mod._log_event = capture_log
        try:
            fake = _FakeOrch()
            now = 1_000_000.0
            silence = STUCK_THRESHOLD_S + 42
            pane = _FakePane(state="working", last_out=now - silence)
            _recover(fake, "backend", "p", pane, now)
        finally:
            orch_mod._log_event = orig_log

        stuck_log = next((e for e in logged if e.get("event") == "stuck_pane_recover"), None)
        assert stuck_log is not None, "stuck_pane_recover event not logged"
        assert stuck_log["silent_for_s"] == int(silence), (
            f"expected {int(silence)}, got {stuck_log['silent_for_s']}"
        )


class _CapOrch(_FakeOrch):
    """_FakeOrch + the bits _give_up_stuck touches, so the STUCK_RECOVER_MAX
    cap path (#41) runs for real against the watchdog driver."""

    def __init__(self) -> None:
        super().__init__()
        self._pipeline_runs: dict = {}
        self.leadInjected = MagicMock()
        self.advance_calls: list[tuple] = []
        # _give_up_stuck now releases the auto-chain via this helper (bug-1 orch);
        # track fires so a give-up of the last tagged pane can be asserted.
        self._inject_auto_chain_handoff = MagicMock()

    def _project_panes(self, project: str | None):
        return self._panes_by_project.get(project or "", {})

    def _advance_pipeline(self, project, pl_key, run) -> None:
        self.advance_calls.append((project, pl_key, run))

    def _maybe_fire_auto_chain_handoff(self, project, was_auto_chain) -> None:
        Orchestrator._maybe_fire_auto_chain_handoff(self, project, was_auto_chain)  # type: ignore[arg-type]

    def _give_up_stuck(self, role, project, pane, now) -> None:
        Orchestrator._give_up_stuck(self, role, project, pane, now)  # type: ignore[arg-type]


def _drive_until(fake: _FakeOrch, pane, ticks: int, start: float = 1_000_000.0) -> float:
    """Run `_check` *ticks* times, stepping past the cooldown each tick and
    re-staling the pane so the watchdog keeps wanting to recover it."""
    now = start
    for _ in range(ticks):
        pane._last_output_ts = now - STUCK_THRESHOLD_S - 1
        _check(fake, now)
        now += STUCK_RECOVER_COOLDOWN_S + 1
    return now


class TestStuckRecoverCap:
    def test_attempts_increment_each_recover(self) -> None:
        fake = _FakeOrch()
        pane = _FakePane(state="working", last_out=0.0)
        fake._panes_by_project["p"] = {"backend": pane}
        for expected in (1, 2, 3):
            _recover(fake, "backend", "p", pane, 1_000_000.0 + expected)
            assert fake._pane_state["p::backend"].stuck_recover_attempts == expected

    def test_attempts_survive_real_close_pop(self) -> None:
        class _PopOnClose(_FakeOrch):
            def close(
                self, role, project=None, suppress_pipeline=False, suppress_auto_chain=False, **_kw
            ):
                self._pane_state.pop(f"{project or ''}::{role}", None)
                self._idle_state.pop(f"{project or ''}::{role}", None)
                self.close_calls.append((role, project or ""))
                return True, "ok"

        fake = _PopOnClose()
        pane = _FakePane(state="working", last_out=0.0)
        fake._panes_by_project["p"] = {"backend": pane}
        _recover(fake, "backend", "p", pane, 1_000_000.0)
        ps = fake._pane_state.get("p::backend")
        assert ps is not None and ps.stuck_recover_attempts == 1, (
            "stuck_recover_attempts must survive close()->pop()->_do_respawn() "
            "or STUCK_RECOVER_MAX can never bite"
        )

    def test_cap_stops_recovery_loop(self) -> None:
        fake = _CapOrch()
        pane = _FakePane(state="working", last_out=0.0)
        fake._panes_by_project["p"] = {"backend": pane}
        _drive_until(fake, pane, ticks=STUCK_RECOVER_MAX + 4)
        # Exactly STUCK_RECOVER_MAX recoveries fire, then the watchdog gives up.
        assert len(fake.close_calls) == STUCK_RECOVER_MAX
        assert len(fake.spawn_calls) == STUCK_RECOVER_MAX
        assert fake._pane_state["p::backend"].stuck_recover_gave_up is True

    def test_give_up_is_one_shot(self) -> None:
        fake = _CapOrch()
        pane = _FakePane(state="working", last_out=0.0)
        fake._panes_by_project["p"] = {"backend": pane}
        _drive_until(fake, pane, ticks=STUCK_RECOVER_MAX + 1)  # reach + trigger give-up
        log_count_before = len(fake.spawn_calls)
        # Many more ticks must not recover again nor advance/warn repeatedly.
        _drive_until(fake, pane, ticks=10, start=5_000_000.0)
        assert len(fake.spawn_calls) == log_count_before
        assert len(fake.close_calls) == STUCK_RECOVER_MAX

    def test_cap_fails_and_advances_pipeline(self) -> None:
        fake = _CapOrch()
        pane = _FakePane(state="working", last_out=0.0)
        fake._panes_by_project["p"] = {"backend": pane}
        run = PipelineRun(
            run_id="r1", template_id="t", template_name="T", hops=[[{"role": "backend"}]]
        )
        run.hop_pending = {"backend"}
        fake._pipeline_runs["p::r1"] = run
        # Tie the pane to the pipeline run (set on the persisted PaneState).
        fake._ps("p::backend").pipeline_run_id = "r1"
        _drive_until(fake, pane, ticks=STUCK_RECOVER_MAX + 2)
        assert "backend" in run.hop_failed
        assert "backend" not in run.hop_pending
        assert len(fake.advance_calls) == 1, "hop emptied → pipeline must advance once"
        # The surviving pane must be UNLINKED from the run so a later operator
        # close() can't re-enter the pipeline branch and spuriously advance again.
        assert fake._pane_state["p::backend"].pipeline_run_id is None

    def test_give_up_warns_lead(self) -> None:
        fake = _CapOrch()
        pane = _FakePane(state="working", last_out=0.0)
        lead = _FakePane(state="working", last_out=0.0)
        fake._panes_by_project["p"] = {"backend": pane, LEAD.name: lead}
        _drive_until(fake, pane, ticks=STUCK_RECOVER_MAX + 2)
        assert fake.notify_calls, "Lead must be warned when a pane is stuck-capped"
        project, notice, _from_role = fake.notify_calls[0]
        assert project == "p"
        assert "stuck-capped" in notice

    def test_give_up_last_auto_chain_pane_releases_handoff(self) -> None:
        # bug-1 orch: if the stuck-capped pane was the last auto-chain blocker,
        # the verify hop must be released (not deadlock waiting for its done).
        fake = _CapOrch()
        pane = _FakePane(state="working", last_out=0.0)
        lead = _FakePane(state="working", last_out=0.0)
        fake._panes_by_project["p"] = {"backend": pane, LEAD.name: lead}
        fake._ps("p::backend").auto_chain = True
        _drive_until(fake, pane, ticks=STUCK_RECOVER_MAX + 2)
        fake._inject_auto_chain_handoff.assert_called_once_with("p")

    def test_give_up_non_auto_chain_pane_no_handoff(self) -> None:
        fake = _CapOrch()
        pane = _FakePane(state="working", last_out=0.0)
        lead = _FakePane(state="working", last_out=0.0)
        fake._panes_by_project["p"] = {"backend": pane, LEAD.name: lead}
        _drive_until(fake, pane, ticks=STUCK_RECOVER_MAX + 2)
        fake._inject_auto_chain_handoff.assert_not_called()


class TestResumeNudgeAndLog:
    """A: a resumed stuck-recovery must drive the pane to continue (a short
    nudge), not leave it idle at the ready prompt. Blank recovery still
    re-pastes the full task. C: the recover event logs content_static_s
    (the real trigger) alongside the (often-zero) raw silent_for_s."""

    def test_resumed_recovery_sends_continue_nudge(self) -> None:
        from agent_takkub.orchestrator import _STUCK_RESUME_NUDGE

        sent: list[tuple[str, str]] = []

        class _ResumeOrch(_FakeOrch):
            def spawn(self, role, cwd=None, project=None, **_kw):
                # Simulate a --resume respawn by setting the structured flag
                # _auto_recover_stuck reads to decide re-paste vs. nudge.
                self._ps(f"{project or ''}::{role}").last_spawn_resumed = True
                return super().spawn(role, cwd=cwd, project=project, **_kw)

            def _send_when_ready(self, role, task, project=None):
                sent.append((role, task))

        fake = _ResumeOrch()
        now = 1_000_000.0
        ps = fake._ps("p::backend")
        ps.last_assigned_task = "implement /foo"
        ps.session_uuid = "uuid-123"
        ps.session_uuid_cwd = "/x"
        pane = _FakePane(state="working", last_out=now - STUCK_THRESHOLD_S - 1)
        _recover(fake, "backend", "p", pane, now)

        assert sent == [("backend", _STUCK_RESUME_NUDGE)], (
            "resumed recovery must send the short continue-nudge so the pane "
            "keeps working, not sit idle"
        )

    def test_blank_recovery_repastes_full_task(self) -> None:
        sent: list[tuple[str, str]] = []

        class _BlankOrch(_FakeOrch):
            # spawn leaves last_spawn_resumed at its PaneState default (False).
            def _send_when_ready(self, role, task, project=None):
                sent.append((role, task))

        fake = _BlankOrch()
        now = 1_000_000.0
        fake._ps("p::backend").last_assigned_task = "implement /foo"
        pane = _FakePane(state="working", last_out=now - STUCK_THRESHOLD_S - 1)
        _recover(fake, "backend", "p", pane, now)

        assert sent == [("backend", "implement /foo")], (
            "blank (non-resumed) recovery must re-paste the original task verbatim"
        )

    def test_content_static_s_logged_alongside_silent_for_s(self) -> None:
        import agent_takkub.orchestrator as orch_mod

        logged: list[dict] = []
        orig = orch_mod._log_event
        orch_mod._log_event = lambda event, **kw: logged.append({"event": event, **kw})
        try:
            fake = _FakeOrch()
            now = 1_000_000.0
            ps = fake._ps("p::backend")
            ps.last_content_change_ts = now - 123.0
            pane = _FakePane(state="working", last_out=now - 5)
            _recover(fake, "backend", "p", pane, now)
        finally:
            orch_mod._log_event = orig

        rec = next((e for e in logged if e["event"] == "stuck_pane_recover"), None)
        assert rec is not None, "stuck_pane_recover event not logged"
        assert rec["content_static_s"] == 123, "content_static_s = real trigger duration"
        assert rec["silent_for_s"] == 5, "raw-byte silence still reported for context"


class TestTtyBlockStuckDefer:
    """Issue #54: stuck-recover must be deferred when the pane is blocked on an
    interactive TTY prompt — close→respawn won't fix a subprocess prompt."""

    def test_tty_blocked_pane_defers_stuck_recover(self) -> None:
        """(c) A pane blocked on y/N must NOT be close→respawned by stuck-recover."""
        fake = _FakeOrch()
        now = 1_000_000.0
        pane = _FakePane(state="working", last_out=now - STUCK_THRESHOLD_S - 1)
        pane.session.is_blocked_on_tty_prompt.return_value = "Ok to proceed? (y)"
        fake._panes_by_project["p"] = {"backend": pane}
        # Seed the content-change ts so the threshold check fires.
        fake._ps("p::backend").last_content_change_ts = now - STUCK_THRESHOLD_S - 1
        _check(fake, now)
        assert fake.close_calls == [], "TTY-blocked pane must NOT be closed/respawned"
        assert fake.spawn_calls == []

    def test_tty_blocked_pane_surfaces_notice_immediately_in_stuck_context(self) -> None:
        """(c) In the stuck-watchdog path we're already past STUCK_THRESHOLD_S (10 min),
        so the notice fires on first detection without waiting TTY_BLOCK_SURFACE_AFTER_S."""
        fake = _FakeOrch()
        now = 1_000_000.0
        pane = _FakePane(state="working", last_out=now - STUCK_THRESHOLD_S - 1)
        pane.session.is_blocked_on_tty_prompt.return_value = "[y/N]"
        fake._panes_by_project["p"] = {"backend": pane}
        fake._ps("p::backend").last_content_change_ts = now - STUCK_THRESHOLD_S - 1

        _check(fake, now)
        assert len(fake.tty_surface_calls) == 1
        assert fake.tty_surface_calls[0][0] == "backend"
        assert "[y/N]" in fake.tty_surface_calls[0][2]
        # Still no recovery fired.
        assert fake.close_calls == []

    def test_tty_surface_notice_respects_cooldown(self) -> None:
        """(c) Surface notice must not spam — respects TTY_BLOCK_SURFACE_COOLDOWN_S."""
        fake = _FakeOrch()
        now = 1_000_000.0
        pane = _FakePane(state="working", last_out=now - STUCK_THRESHOLD_S - 1)
        pane.session.is_blocked_on_tty_prompt.return_value = "Enter passphrase:"
        fake._panes_by_project["p"] = {"backend": pane}
        fake._ps("p::backend").last_content_change_ts = now - STUCK_THRESHOLD_S - 1

        # First surface fires immediately (stuck path, no TTY_BLOCK_SURFACE_AFTER_S wait).
        _check(fake, now)
        assert len(fake.tty_surface_calls) == 1

        # Inside cooldown — no re-surface.
        now2 = now + TTY_BLOCK_SURFACE_COOLDOWN_S - 1
        pane._last_output_ts = now2 - STUCK_THRESHOLD_S - 1
        fake._ps("p::backend").last_content_change_ts = now2 - STUCK_THRESHOLD_S - 1
        _check(fake, now2)
        assert len(fake.tty_surface_calls) == 1

        # After cooldown — surface again.
        now3 = now + TTY_BLOCK_SURFACE_COOLDOWN_S + 1
        pane._last_output_ts = now3 - STUCK_THRESHOLD_S - 1
        fake._ps("p::backend").last_content_change_ts = now3 - STUCK_THRESHOLD_S - 1
        _check(fake, now3)
        assert len(fake.tty_surface_calls) == 2

    def test_non_blocked_pane_still_recovers(self) -> None:
        """(d) Regression: a pane with content-static > threshold and no TTY block
        must still trigger the normal stuck-recover path."""
        fake = _FakeOrch()
        now = 1_000_000.0
        pane = _FakePane(state="working", last_out=now - STUCK_THRESHOLD_S - 1)
        # Explicit None — not TTY-blocked
        pane.session.is_blocked_on_tty_prompt.return_value = None
        fake._panes_by_project["agent-takkub"] = {"backend": pane}
        fake._ps("agent-takkub::backend").last_content_change_ts = now - STUCK_THRESHOLD_S - 1
        _check(fake, now)
        assert len(fake.close_calls) == 1, "non-blocked stuck pane must still be recovered"
        assert fake.spawn_calls[0][0] == "backend"


# ─────────────────────────────────────────────────────────────
# Issue #104: Windows Open-With dialog transcript tripwire
# ─────────────────────────────────────────────────────────────


class TestShellOpenDialogTripwire:
    """Issue #199: a plain substring match on the marker text used to be
    sufficient to notify Lead — which meant the tripwire matched its OWN
    notify message (an f-string quoting that exact text) whenever a pane
    edited or `Read` the source line that builds it. Every "true positive"
    test here now requires BOTH silence (>= _SHELL_DIALOG_IDLE_GATE_S, a
    real dialog freezes the process — an actively-working pane can't be
    blocked on one) AND a mocked corroborating OpenWith/AppPicker process,
    matching the real fix's two independent gates."""

    def _pane_with_transcript(self, tmp_path, text: str, **kw):
        pane = _FakePane(**kw)
        p = tmp_path / "transcript.log"
        p.write_text(text, encoding="utf-8")
        pane._transcript_path = str(p)
        return pane

    @pytest.fixture(autouse=True)
    def _corroborated_windows_dialog(self, monkeypatch: pytest.MonkeyPatch):
        """Default stand-in for a real corroborating OS signal: pretend
        we're on Windows with an OpenWith.exe process actually running.
        Individual tests override this to prove the negative paths."""
        monkeypatch.setattr("agent_takkub.orchestrator.sys.platform", "win32")
        monkeypatch.setattr(
            "agent_takkub.orchestrator._open_with_dialog_process_present", lambda: True
        )

    def _silent_for(self, now: float) -> float:
        """`_last_output_ts` far enough in the past to clear the #199 idle gate."""
        from agent_takkub.orchestrator import _SHELL_DIALOG_IDLE_GATE_S

        return now - _SHELL_DIALOG_IDLE_GATE_S - 1

    def test_marker_present_notifies_lead_once(self, tmp_path) -> None:
        fake = _FakeOrch()
        now = 1_000_000.0
        pane = self._pane_with_transcript(
            tmp_path,
            "some output\nHow do you want to open this file?\nmore output",
            last_out=self._silent_for(now),
        )
        fake._panes_by_project["p"] = {"backend": pane}

        _check(fake, now)

        assert len(fake.notify_calls) == 1
        project, notice, from_role = fake.notify_calls[0]
        assert project == "p"
        assert from_role == "backend"
        assert "How do you want to open this file?" in notice
        assert "#104" in notice

    def test_marker_dedupes_across_ticks(self, tmp_path) -> None:
        fake = _FakeOrch()
        now = 1_000_000.0
        pane = self._pane_with_transcript(
            tmp_path,
            "How do you want to open this file?",
            last_out=self._silent_for(now),
        )
        fake._panes_by_project["p"] = {"backend": pane}

        _check(fake, now)
        _check(fake, now + 5)
        _check(fake, now + 10)

        assert len(fake.notify_calls) == 1, "must nudge Lead once per pane, not every tick"

    def test_no_marker_no_notification(self, tmp_path) -> None:
        fake = _FakeOrch()
        now = 1_000_000.0
        pane = self._pane_with_transcript(
            tmp_path,
            "normal claude output\nno dialogs here",
            last_out=self._silent_for(now),
        )
        fake._panes_by_project["p"] = {"backend": pane}

        _check(fake, now)

        assert fake.notify_calls == []

    def test_missing_transcript_path_is_noop(self) -> None:
        """No `_transcript_path` attribute at all (e.g. bootstrap) — must not
        raise or notify, just skip (mirrors `_scan_done_evidence` degrade)."""
        fake = _FakeOrch()
        now = 1_000_000.0
        pane = _FakePane(state="working", last_out=self._silent_for(now))
        fake._panes_by_project["p"] = {"backend": pane}

        _check(fake, now)

        assert fake.notify_calls == []

    def test_active_progress_suppresses_notification(self, tmp_path) -> None:
        """Issue #199 point 4 (the exact false-positive incident): the
        marker text is present, but the pane has recent output — a real
        modal dialog would have frozen it, so this must NOT notify."""
        fake = _FakeOrch()
        now = 1_000_000.0
        pane = self._pane_with_transcript(
            tmp_path,
            "How do you want to open this file?",
            last_out=now - 1,  # progressing normally, nowhere near the idle gate
        )
        fake._panes_by_project["p"] = {"backend": pane}

        _check(fake, now)

        assert fake.notify_calls == [], (
            "an actively-progressing pane can't be blocked on a modal dialog"
        )

    def test_no_corroborating_process_suppresses_notification(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #199 point 1: text match alone is not enough — without a
        real OpenWith/AppPicker process running, don't notify (this is
        exactly what happened in the field: 0 matching processes)."""
        monkeypatch.setattr(
            "agent_takkub.orchestrator._open_with_dialog_process_present", lambda: False
        )
        fake = _FakeOrch()
        now = 1_000_000.0
        pane = self._pane_with_transcript(
            tmp_path,
            "How do you want to open this file?",
            last_out=self._silent_for(now),
        )
        fake._panes_by_project["p"] = {"backend": pane}

        _check(fake, now)

        assert fake.notify_calls == []

    def test_self_reference_line_suppresses_notification(self, tmp_path) -> None:
        """Issue #199 point 2: the exact incident — a matching line that
        reads like the cockpit's own notify-message source (quoted f-string)
        must be treated as self-reference, not a live dialog."""
        fake = _FakeOrch()
        now = 1_000_000.0
        pane = self._pane_with_transcript(
            tmp_path,
            "f\"[cockpit] backend pane อาจติด Windows 'How do you want to open this file?' \"",
            last_out=self._silent_for(now),
        )
        fake._panes_by_project["p"] = {"backend": pane}

        _check(fake, now)

        assert fake.notify_calls == []

    def test_non_windows_never_notifies(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        """This dialog type is Windows-only; on any other platform the
        tripwire must never notify, corroboration process or not."""
        monkeypatch.setattr("agent_takkub.orchestrator.sys.platform", "darwin")
        fake = _FakeOrch()
        now = 1_000_000.0
        pane = self._pane_with_transcript(
            tmp_path,
            "How do you want to open this file?",
            last_out=self._silent_for(now),
        )
        fake._panes_by_project["p"] = {"backend": pane}

        _check(fake, now)

        assert fake.notify_calls == []


class TestShellOpenDialogScanThrottle:
    """Issue #194: the transcript-tail read that backs the tripwire above
    used to run on EVERY 5s watchdog tick for EVERY working pane, blocking
    the Qt main thread for up to 2.5s when the file open() got caught by AV
    scanning. These pin down the fix: throttled re-scan cadence, and the
    scan result marshalled back to the caller via QTimer.singleShot rather
    than notifying inline (proving the read itself is off the calling path)."""

    def _pane_with_transcript(self, tmp_path, text: str, **kw):
        pane = _FakePane(**kw)
        p = tmp_path / "transcript.log"
        p.write_text(text, encoding="utf-8")
        pane._transcript_path = str(p)
        return pane

    def _silent_for(self, now: float) -> float:
        """`_last_output_ts` far enough in the past to clear the #199 idle
        gate — these tests exercise the throttle/threading logic, which
        only runs once the idle gate has already let the call through."""
        from agent_takkub.orchestrator import _SHELL_DIALOG_IDLE_GATE_S

        return now - _SHELL_DIALOG_IDLE_GATE_S - 1

    def test_no_rescan_within_throttle_interval(self, tmp_path, monkeypatch) -> None:
        fake = _FakeOrch()
        now = 1_000_000.0
        pane = self._pane_with_transcript(
            tmp_path, "normal output, no dialog", last_out=self._silent_for(now)
        )
        fake._panes_by_project["p"] = {"backend": pane}

        calls: list[str] = []
        monkeypatch.setattr(
            "agent_takkub.orchestrator._read_tail_bytes",
            lambda path, max_bytes: calls.append(str(path)) or b"normal output, no dialog",
        )

        _check(fake, now)
        _check(fake, now + 5)
        _check(fake, now + 10)

        assert len(calls) == 1, "must not re-open the transcript file every 5s tick"

    def test_rescans_after_throttle_interval_elapses(self, tmp_path, monkeypatch) -> None:
        from agent_takkub.orchestrator import _SHELL_DIALOG_SCAN_INTERVAL_S

        fake = _FakeOrch()
        now = 1_000_000.0
        pane = self._pane_with_transcript(
            tmp_path, "normal output, no dialog", last_out=self._silent_for(now)
        )
        fake._panes_by_project["p"] = {"backend": pane}

        calls: list[str] = []
        monkeypatch.setattr(
            "agent_takkub.orchestrator._read_tail_bytes",
            lambda path, max_bytes: calls.append(str(path)) or b"normal output, no dialog",
        )

        _check(fake, now)
        _check(fake, now + _SHELL_DIALOG_SCAN_INTERVAL_S + 1)

        assert len(calls) == 2, "must resume scanning once the throttle window has elapsed"

    def test_notify_dispatched_via_qtimer_not_inline(
        self, tmp_path, monkeypatch, _patch_qtimer
    ) -> None:
        """The scan runs on a background thread (a real thread in prod); the
        Lead notify must be marshalled back to the caller via
        QTimer.singleShot(0, ...) rather than called directly off-thread."""
        monkeypatch.setattr("agent_takkub.orchestrator.sys.platform", "win32")
        monkeypatch.setattr(
            "agent_takkub.orchestrator._open_with_dialog_process_present", lambda: True
        )
        fake = _FakeOrch()
        now = 1_000_000.0
        pane = self._pane_with_transcript(
            tmp_path, "How do you want to open this file?", last_out=self._silent_for(now)
        )
        fake._panes_by_project["p"] = {"backend": pane}

        _check(fake, now)

        assert len(fake.notify_calls) == 1
        assert any(ms == 0 for ms, _fn in _patch_qtimer)

    def test_in_flight_flag_cleared_after_scan_completes(self, tmp_path) -> None:
        fake = _FakeOrch()
        now = 1_000_000.0
        pane = self._pane_with_transcript(
            tmp_path, "normal output, no dialog", last_out=self._silent_for(now)
        )
        fake._panes_by_project["p"] = {"backend": pane}

        _check(fake, now)

        ps = fake._pane_state["p::backend"]
        assert ps.dialog_scan_in_flight is False
        assert ps.last_dialog_scan_ts == now
