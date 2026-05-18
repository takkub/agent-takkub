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
        self._panes_by_project: dict[str, dict] = {}
        self._last_stuck_recover: dict[str, float] = {}
        self.close_calls: list[tuple[str, str]] = []
        self.spawn_calls: list[tuple[str, str | None, str]] = []

    def close(self, role: str, project: str | None = None) -> tuple[bool, str]:
        self.close_calls.append((role, project or ""))
        return True, "ok"

    def spawn(self, role: str, cwd: str | None = None, project: str | None = None):
        self.spawn_calls.append((role, cwd, project or ""))
        return True, "ok"

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
        assert fake._last_stuck_recover["agent-takkub::backend"] == now

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
        assert fake._last_stuck_recover["p::backend"] == now
