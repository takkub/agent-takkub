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
    STUCK_THRESHOLD_S,
    Orchestrator,
    PaneState,
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

    def _ps(self, key: str) -> PaneState:
        try:
            return self._pane_state[key]
        except KeyError:
            ps = PaneState()
            self._pane_state[key] = ps
            return ps

    def close(self, role: str, project: str | None = None) -> tuple[bool, str]:
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
            def close(self, role: str, project: str | None = None) -> tuple[bool, str]:
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
            def close(self, role: str, project: str | None = None) -> tuple[bool, str]:
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
