"""Targeted tests for #537:

`done()`'s 2.5s auto-close used to kill whatever subprocess tree was still
running under the pane unconditionally, with `_warn_if_live_children` only
*announcing* the kill a moment ahead of `terminate()` — no actual delay. A
still-writing evidence-collection script (or any other legitimate in-flight
work) was killed mid-write on the very report that warned about it.

`_close_if_same_session` (the auto-close callback `done()` schedules) now
defers the close while real (non-scaffolding) subprocesses are still visible
under the pane, polling every `DONE_CLOSE_LIVE_CHILD_POLL_MS` instead of
blocking the Qt thread, bounded by `DONE_CLOSE_LIVE_CHILD_GRACE_S` so a child
that is itself hung can't pin the pane open forever.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import (
    DONE_CLOSE_LIVE_CHILD_GRACE_S,
    DONE_CLOSE_LIVE_CHILD_POLL_MS,
    Orchestrator,
)

TEST_PROJECT = "done-close-defer-test"


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
    # `_close_if_same_session` re-checks `pane.state == "done"` on every poll
    # tick, so (unlike other done()-gate tests, which never invoke that
    # callback) set_state must actually mutate `.state` here, not just record
    # the call.
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
    """Register *role* + lead, assign a task, call done(), and return
    ``(lead_pane, close_callback)`` — *close_callback* is the 2.5s
    auto-close callback `done()` scheduled, picked out of every
    QTimer.singleShot call captured into *timers* (done()/assign() can
    schedule other, unrelated timers too — e.g. a 200ms verified-enter
    check — so the auto-close one must be found by its ms, not by
    position)."""
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
    return lead, close_cb


class TestDoneCloseDefersForLiveChildren:
    def test_no_live_children_closes_immediately(self, orch: Orchestrator) -> None:
        timers: list = []
        _lead, close_cb = _assign_and_done(orch, "devops", timers)

        with (
            patch.object(orch, "_live_non_scaffolding_children", return_value=[]),
            patch.object(orch, "close") as close_mock,
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
        ):
            close_cb()  # fire the 2.5s auto-close callback

        close_mock.assert_called_once_with("devops", project=TEST_PROJECT)

    def test_live_children_defer_close_and_notify_lead_once(self, orch: Orchestrator) -> None:
        timers: list = []
        _lead, close_cb = _assign_and_done(orch, "qa", timers)

        with (
            patch.object(orch, "_live_non_scaffolding_children", return_value=["node"]),
            patch.object(orch, "close") as close_mock,
            patch.object(orch, "_notify_lead") as notify_mock,
        ):
            poll_timers: list = []
            with patch(
                "agent_takkub.orchestrator.QTimer.singleShot",
                side_effect=lambda ms, cb: poll_timers.append((ms, cb)),
            ):
                close_cb()  # first tick: children alive → must defer, not close

            close_mock.assert_not_called()
            poll_cb = next(cb for ms, cb in poll_timers if ms == DONE_CLOSE_LIVE_CHILD_POLL_MS)
            assert notify_mock.call_count == 1
            assert notify_mock.call_args.kwargs.get("note") == "done_close_deferred"
            assert "เลื่อนการปิด" in notify_mock.call_args.args[1]

            # A second poll tick with children still alive must NOT close and
            # must NOT re-notify Lead (one notice per deferral episode).
            poll_timers2: list = []
            with patch(
                "agent_takkub.orchestrator.QTimer.singleShot",
                side_effect=lambda ms, cb: poll_timers2.append((ms, cb)),
            ):
                poll_cb()

            close_mock.assert_not_called()
            assert notify_mock.call_count == 1
            assert any(ms == DONE_CLOSE_LIVE_CHILD_POLL_MS for ms, _cb in poll_timers2)

    def test_close_proceeds_once_children_clear(self, orch: Orchestrator) -> None:
        timers: list = []
        _lead, close_cb = _assign_and_done(orch, "backend", timers)

        poll_timers: list = []
        with (
            patch.object(orch, "_live_non_scaffolding_children", return_value=["node"]),
            patch.object(orch, "close") as close_mock,
            patch(
                "agent_takkub.orchestrator.QTimer.singleShot",
                side_effect=lambda ms, cb: poll_timers.append((ms, cb)),
            ),
        ):
            close_cb()
        close_mock.assert_not_called()
        poll_cb = next(cb for ms, cb in poll_timers if ms == DONE_CLOSE_LIVE_CHILD_POLL_MS)

        # Children have finished by the next poll tick.
        with (
            patch.object(orch, "_live_non_scaffolding_children", return_value=[]),
            patch.object(orch, "close") as close_mock2,
        ):
            poll_cb()
        close_mock2.assert_called_once_with("backend", project=TEST_PROJECT)

    def test_grace_period_expiry_falls_back_to_kill(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A child that is itself hung must not pin the pane open forever —
        past DONE_CLOSE_LIVE_CHILD_GRACE_S the close proceeds anyway."""
        timers: list = []
        _lead, cb = _assign_and_done(orch, "frontend", timers)

        clock = {"t": 1_000_000.0}
        monkeypatch.setattr("agent_takkub.orchestrator.time.time", lambda: clock["t"])

        with patch.object(orch, "_live_non_scaffolding_children", return_value=["docker"]):
            # A few ticks, each well inside the grace window, must keep deferring.
            for _ in range(3):
                poll_timers: list = []

                def _capture(ms, next_cb, _pt=poll_timers):
                    _pt.append((ms, next_cb))

                with (
                    patch.object(orch, "close") as close_mock,
                    patch("agent_takkub.orchestrator.QTimer.singleShot", side_effect=_capture),
                ):
                    cb()
                    close_mock.assert_not_called()
                clock["t"] += 60.0
                cb = next(c for ms, c in poll_timers if ms == DONE_CLOSE_LIVE_CHILD_POLL_MS)

            # Now jump well past the grace window: must give up deferring.
            clock["t"] += DONE_CLOSE_LIVE_CHILD_GRACE_S + 60.0
            with patch.object(orch, "close") as final_close_mock:
                cb()
            final_close_mock.assert_called_once_with("frontend", project=TEST_PROJECT)

    def test_reassigned_pane_aborts_deferred_close(self, orch: Orchestrator) -> None:
        """If the pane picks up a new task while a close is deferred, the
        stale callback must never close the pane out from under the new
        work — same guarantee the original session/state check already gave
        the un-deferred path."""
        timers: list = []
        _lead, close_cb = _assign_and_done(orch, "mobile", timers)

        poll_timers: list = []
        with (
            patch.object(orch, "_live_non_scaffolding_children", return_value=["gradle"]),
            patch.object(orch, "close"),
            patch(
                "agent_takkub.orchestrator.QTimer.singleShot",
                side_effect=lambda ms, cb: poll_timers.append((ms, cb)),
            ),
        ):
            close_cb()
        poll_cb = next(cb for ms, cb in poll_timers if ms == DONE_CLOSE_LIVE_CHILD_POLL_MS)

        orch._panes_by_project[TEST_PROJECT]["mobile"].state = "working"

        with patch.object(orch, "close") as close_mock:
            poll_cb()
        close_mock.assert_not_called()
