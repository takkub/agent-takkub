"""Targeted tests for #554 (follow-up to #537):

`_close_if_same_session`'s live-children defer loop (#537) bounds how long it
waits for a still-running subprocess by `DONE_CLOSE_LIVE_CHILD_GRACE_S` (up to
15 min), but that grace period can't tell a genuinely still-running
subprocess apart from an orphaned wrapper shell that will never exit on its
own (real report: a leftover bash.exe left running under a devops pane after
its actual task, and its done() report, had already succeeded). That shape
sat out the full grace period every time, reading to the user as "stuck
again" and requiring a manual `takkub close`.

`_close_if_same_session` now short-circuits the wait once the live children
have shown identical CPU time across consecutive poll ticks (no CPU
activity, no new/exited children) AND the pane's PTY has produced no new
output for `DONE_CLOSE_IDLE_CHILD_THRESHOLD_S` — closing right away instead
of waiting out the rest of `DONE_CLOSE_LIVE_CHILD_GRACE_S`. Real, varying
subprocess activity (changing CPU time, or fresh PTY output) must keep
deferring exactly as #537 already covers.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import (
    DONE_CLOSE_IDLE_CHILD_THRESHOLD_S,
    DONE_CLOSE_LIVE_CHILD_POLL_MS,
    Orchestrator,
)

TEST_PROJECT = "done-close-idle-test"


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


def _make_working_pane(cwd: str = "/repo") -> MagicMock:
    pane = MagicMock()
    pane.state = "working"
    pane.session = MagicMock()
    pane.session.is_alive = True
    pane.session.is_at_ready_prompt.return_value = True
    pane._session_cwd = cwd
    pane._transcript_path = None
    pane.set_state = MagicMock(
        side_effect=lambda new_state, note=None: setattr(pane, "state", new_state)
    )
    return pane


def _make_lead_pane() -> MagicMock:
    pane = MagicMock()
    pane.session = MagicMock()
    pane.session.is_alive = True
    return pane


def _assign_and_done(orch: Orchestrator, role: str, timers: list):
    pane = _make_working_pane()
    orch._panes_by_project.setdefault(TEST_PROJECT, {})[role] = pane
    lead = _make_lead_pane()
    orch._panes_by_project[TEST_PROJECT]["lead"] = lead
    with (
        patch.object(orch, "spawn", return_value=(True, "spawned")),
        patch.object(orch, "_send_when_ready"),
    ):
        orch.assign(role, cwd="/repo", task="do work", project=TEST_PROJECT)
    with patch(
        "agent_takkub.orchestrator.QTimer.singleShot",
        side_effect=lambda ms, cb: timers.append((ms, cb)),
    ):
        ok, _msg = orch.done(role, note="done", project=TEST_PROJECT)
    assert ok is True
    close_cb = next(cb for ms, cb in timers if ms == 2_500)
    return pane, close_cb


def _next_poll_cb(poll_timers: list):
    return next(cb for ms, cb in poll_timers if ms == DONE_CLOSE_LIVE_CHILD_POLL_MS)


class TestIdleOrphanChildrenShortCircuit:
    def test_idle_orphan_closes_before_full_grace_period(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        timers: list = []
        pane, close_cb = _assign_and_done(orch, "devops", timers)

        clock = {"t": 1_000_000.0}
        monkeypatch.setattr("agent_takkub.orchestrator.time.time", lambda: clock["t"])
        pane.session.seconds_since_output.return_value = 30.0  # PTY long quiet

        with (
            patch.object(orch, "_live_non_scaffolding_children", return_value=["bash"]),
            patch.object(
                orch,
                "_live_non_scaffolding_children_cpu_snapshot",
                return_value={123: 0.5},  # constant across ticks: no CPU used
            ),
        ):
            # Tick 1: first sighting, nothing to compare against yet -> defer.
            poll_timers: list = []
            with (
                patch.object(orch, "close") as close_mock,
                patch(
                    "agent_takkub.orchestrator.QTimer.singleShot",
                    side_effect=lambda ms, cb: poll_timers.append((ms, cb)),
                ),
            ):
                close_cb()
            close_mock.assert_not_called()
            poll_cb = _next_poll_cb(poll_timers)

            # Tick 2: CPU snapshot now comparable to tick 1's and identical ->
            # idle streak starts, but hasn't lasted DONE_CLOSE_IDLE_CHILD_THRESHOLD_S yet.
            clock["t"] += 5.0
            poll_timers2: list = []
            with (
                patch.object(orch, "close") as close_mock2,
                patch(
                    "agent_takkub.orchestrator.QTimer.singleShot",
                    side_effect=lambda ms, cb: poll_timers2.append((ms, cb)),
                ),
            ):
                poll_cb()
            close_mock2.assert_not_called()
            poll_cb2 = _next_poll_cb(poll_timers2)

            # Tick 3: idle streak has now lasted past the threshold -> close
            # right away, nowhere near the 15-minute grace period.
            clock["t"] += DONE_CLOSE_IDLE_CHILD_THRESHOLD_S + 1.0
            with patch.object(orch, "close") as close_mock3:
                poll_cb2()
            close_mock3.assert_called_once_with("devops", project=TEST_PROJECT)

    def test_active_cpu_usage_keeps_deferring(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Real work (changing CPU time each tick) must never be short-circuited."""
        timers: list = []
        _pane, close_cb = _assign_and_done(orch, "qa", timers)

        clock = {"t": 1_000_000.0}
        monkeypatch.setattr("agent_takkub.orchestrator.time.time", lambda: clock["t"])
        cpu_ticks = [{456: 1.0}, {456: 2.5}, {456: 4.0}, {456: 5.5}]

        with (
            patch.object(orch, "_live_non_scaffolding_children", return_value=["node"]),
            patch.object(
                orch,
                "_live_non_scaffolding_children_cpu_snapshot",
                side_effect=cpu_ticks,
            ),
        ):
            cb = close_cb
            for _ in range(len(cpu_ticks)):
                clock["t"] += DONE_CLOSE_IDLE_CHILD_THRESHOLD_S + 5.0
                poll_timers: list = []
                with (
                    patch.object(orch, "close") as close_mock,
                    patch(
                        "agent_takkub.orchestrator.QTimer.singleShot",
                        side_effect=lambda ms, cb_, _pt=poll_timers: _pt.append((ms, cb_)),
                    ),
                ):
                    cb()
                close_mock.assert_not_called()
                cb = _next_poll_cb(poll_timers)

    def test_recent_pty_output_keeps_deferring_despite_idle_cpu(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Identical CPU snapshots alone must not be enough — fresh PTY
        output (below the poll interval) means the pane is not actually
        idle yet."""
        timers: list = []
        pane, close_cb = _assign_and_done(orch, "backend", timers)

        clock = {"t": 1_000_000.0}
        monkeypatch.setattr("agent_takkub.orchestrator.time.time", lambda: clock["t"])
        pane.session.seconds_since_output.return_value = 0.5  # actively producing output

        with (
            patch.object(orch, "_live_non_scaffolding_children", return_value=["python"]),
            patch.object(
                orch,
                "_live_non_scaffolding_children_cpu_snapshot",
                return_value={789: 3.0},
            ),
        ):
            cb = close_cb
            for _ in range(3):
                clock["t"] += DONE_CLOSE_IDLE_CHILD_THRESHOLD_S + 5.0
                poll_timers: list = []
                with (
                    patch.object(orch, "close") as close_mock,
                    patch(
                        "agent_takkub.orchestrator.QTimer.singleShot",
                        side_effect=lambda ms, cb_, _pt=poll_timers: _pt.append((ms, cb_)),
                    ),
                ):
                    cb()
                close_mock.assert_not_called()
                cb = _next_poll_cb(poll_timers)
