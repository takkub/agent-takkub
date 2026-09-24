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

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import (
    DONE_CLOSE_LIVE_CHILD_GRACE_S,
    DONE_CLOSE_LIVE_CHILD_POLL_MS,
    Orchestrator,
    _exit_key,
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
    # 2.1.17 keeps panes alive after done by default (pane reuse); the
    # deferred-close logic under test only runs on the opt-in auto-close.
    monkeypatch.setattr("agent_takkub.orchestrator.CLOSE_ON_DONE", True)
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

        close_mock.assert_called_once_with("devops", project=TEST_PROJECT, preserve_resume=True)

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
            # Item 2 (#635): do NOT send separate _notify_lead message for deferred close
            # (info is appended to the saved note instead, merged into digest)
            assert notify_mock.call_count == 0

            # A second poll tick with children still alive must NOT close and
            # must NOT send any notification (deferred info already in note from first tick).
            poll_timers2: list = []
            with patch(
                "agent_takkub.orchestrator.QTimer.singleShot",
                side_effect=lambda ms, cb: poll_timers2.append((ms, cb)),
            ):
                poll_cb()

            close_mock.assert_not_called()
            assert notify_mock.call_count == 0
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
        close_mock2.assert_called_once_with("backend", project=TEST_PROJECT, preserve_resume=True)

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
            # #604: the deferred notice already told Lead about these live
            # children when the episode started — the grace-expiry close
            # must suppress `_warn_if_live_children`'s own notice so this
            # episode produces one Lead message total, not two.
            final_close_mock.assert_called_once_with(
                "frontend",
                project=TEST_PROJECT,
                suppress_live_children_warning=True,
                preserve_resume=True,
            )

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


class TestDoneCloseSurvivesNaturalExit:
    """#559: the pane's own underlying process routinely exits on its own
    shortly after `done()` (independent of #537's live-child deferral above)
    — AgentPane._on_exit() reacts by nulling `.session` (detach_session())
    and dropping the pane to "empty". The old guard (`pane.session is not
    _done_sess`) treated that None as "respawned with a different session"
    and silently gave up on ever closing the tab — user-visible as a
    permanently stuck "empty slot" tab. `_close_if_same_session` must still
    finish tearing the pane down in that case, while a *genuine* respawn
    (a new, non-None session object attached to the role) must still abort
    the stale close exactly as before.
    """

    def test_session_gone_and_state_empty_still_closes(self, orch: Orchestrator) -> None:
        timers: list = []
        _lead, close_cb = _assign_and_done(orch, "devops", timers)

        pane = orch._panes_by_project[TEST_PROJECT]["devops"]
        # Simulate AgentPane._on_exit()'s effect when the process exits on
        # its own while this close is still pending.
        pane.session = None
        pane.state = "empty"

        with (
            patch.object(orch, "_live_non_scaffolding_children", return_value=[]),
            patch.object(orch, "close") as close_mock,
        ):
            close_cb()

        close_mock.assert_called_once_with("devops", project=TEST_PROJECT, preserve_resume=True)

    def test_genuine_respawn_with_new_session_still_aborts_close(self, orch: Orchestrator) -> None:
        timers: list = []
        _lead, close_cb = _assign_and_done(orch, "qa", timers)

        pane = orch._panes_by_project[TEST_PROJECT]["qa"]
        # A real respawn: a brand-new, non-None session object attached
        # before the deferred close fires.
        pane.session = MagicMock()
        pane.state = "active"

        with patch.object(orch, "close") as close_mock:
            close_cb()

        close_mock.assert_not_called()


class TestDeferredClosePreservesResume722:
    """Issue #722: Regression tests for #683 session resume after deferred close.

    When done() auto-close is deferred because of live children (e.g. bash.exe),
    the eventual close (whether via children clearing or grace expiring) must
    preserve the session uuid and stamp _recent_exits, so that re-assigning within
    RESUME_WINDOW_SEC resumes the prior Claude session with --resume <uuid>.
    Provider override (--role gemini --provider claude) must also resume when
    the new spawn provider equals the prior session provider, and must never bleed
    Claude's UUID into a non-claude spawn.
    """

    UUID = "e9aaa908-1111-4000-a000-000000000001"

    def test_deferred_close_preserves_resume_and_reassign_resumes(
        self, orch: Orchestrator, tmp_path
    ) -> None:
        orch.paneClosed.connect(
            lambda role, project, o=orch: o.unregister_pane(role, project=project)
        )
        key = _exit_key(TEST_PROJECT, "gemini")
        pane = _make_working_pane(cwd=str(tmp_path))
        pane.model = MagicMock()
        pane.model.provider_name = "claude"
        pane.model.session_uuid = self.UUID
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["gemini"] = pane
        orch._panes_by_project[TEST_PROJECT]["lead"] = _make_lead_pane()

        ps = orch._ps(key)
        ps.session_uuid = self.UUID
        ps.session_uuid_cwd = str(tmp_path)
        ps.session_provider = "claude"
        ps.provider_override = "claude"
        ps.last_assigned_task = "remember word ม่วง-683"
        ps.task_id = "task-1"
        ps.task_delivered = True

        scheduled: list = []
        with patch(
            "agent_takkub.orchestrator.QTimer.singleShot",
            side_effect=lambda ms, cb: scheduled.append((ms, cb)),
        ):
            ok, msg = orch.done("gemini", note="done", project=TEST_PROJECT)
            assert ok, msg

        close_cb = next(cb for ms, cb in scheduled if ms == 2500)

        # First tick: children are still running (bash.exe) -> close is deferred
        poll_timers: list = []
        with (
            patch.object(
                orch, "_live_non_scaffolding_children", return_value=["bash.exe", "bash.exe"]
            ),
            patch(
                "agent_takkub.orchestrator.QTimer.singleShot",
                side_effect=lambda ms, cb: poll_timers.append((ms, cb)),
            ),
        ):
            close_cb()

        poll_cb = next(cb for ms, cb in poll_timers if ms == DONE_CLOSE_LIVE_CHILD_POLL_MS)

        # Second tick: children clear -> close runs for real
        with patch.object(orch, "_live_non_scaffolding_children", return_value=[]):
            poll_cb()

        # Verify close seeded resume state and stamped _recent_exits
        seeded = orch._pane_state.get(key)
        assert seeded is not None
        assert seeded.session_uuid == self.UUID
        assert seeded.session_uuid_cwd == str(tmp_path)
        assert seeded.session_provider == "claude"

        exit_rec = orch._recent_exits.get(key)
        assert exit_rec is not None
        assert exit_rec["cwd"] == str(tmp_path)
        assert exit_rec["provider"] == "claude"

        # Re-assign within RESUME_WINDOW_SEC with --provider claude:
        # spawn argv must contain --resume <self.UUID>
        new_pane = _make_working_pane(cwd=str(tmp_path))
        new_pane.session = None
        new_pane.state = "empty"
        orch.paneRequested.connect(
            lambda role, project, o=orch, p=new_pane: o._panes_by_project.setdefault(
                project, {}
            ).__setitem__(role, p)
        )

        captured_argv: list[list[str]] = []
        fake_sess = MagicMock()
        fake_sess.processExited = MagicMock()
        fake_sess.processExited.connect = MagicMock()

        with (
            patch("agent_takkub.spawn_engine._cwd_within_project", return_value=True),
            patch("agent_takkub.orchestrator.find_claude_executable", return_value="claude"),
            patch.object(orch_mod.PtySession, "__new__", return_value=fake_sess),
            patch.object(
                fake_sess,
                "spawn",
                side_effect=lambda argv, cwd, env, **kwargs: captured_argv.append(list(argv)),
            ),
            patch.object(orch, "_send_when_ready"),
        ):
            ok, msg = orch.assign(
                "gemini",
                cwd=str(tmp_path),
                task="what was the word?",
                provider="claude",
                project=TEST_PROJECT,
            )
            print("ASSIGN RESULT:", ok, msg)
            assert ok, msg
            assert captured_argv
            argv = captured_argv[0]
            assert "--resume" in argv
            assert argv[argv.index("--resume") + 1] == self.UUID
            assert "--session-id" not in argv

    def test_deferred_close_provider_mismatch_does_not_resume_claude_uuid(
        self, orch: Orchestrator, tmp_path
    ) -> None:
        orch.paneClosed.connect(
            lambda role, project, o=orch: o.unregister_pane(role, project=project)
        )
        key = _exit_key(TEST_PROJECT, "gemini")
        pane = _make_working_pane(cwd=str(tmp_path))
        pane.model = MagicMock()
        pane.model.provider_name = "claude"
        pane.model.session_uuid = self.UUID
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["gemini"] = pane
        orch._panes_by_project[TEST_PROJECT]["lead"] = _make_lead_pane()

        ps = orch._ps(key)
        ps.session_uuid = self.UUID
        ps.session_uuid_cwd = str(tmp_path)
        ps.session_provider = "claude"
        ps.provider_override = "claude"
        ps.last_assigned_task = "remember word ม่วง-683"
        ps.task_id = "task-1"
        ps.task_delivered = True

        scheduled: list = []
        with patch(
            "agent_takkub.orchestrator.QTimer.singleShot",
            side_effect=lambda ms, cb: scheduled.append((ms, cb)),
        ):
            orch.done("gemini", note="done", project=TEST_PROJECT)

        close_cb = next(cb for ms, cb in scheduled if ms == 2500)

        poll_timers: list = []
        with (
            patch.object(orch, "_live_non_scaffolding_children", return_value=["bash.exe"]),
            patch(
                "agent_takkub.orchestrator.QTimer.singleShot",
                side_effect=lambda ms, cb: poll_timers.append((ms, cb)),
            ),
        ):
            close_cb()

        poll_cb = next(cb for ms, cb in poll_timers if ms == DONE_CLOSE_LIVE_CHILD_POLL_MS)
        with patch.object(orch, "_live_non_scaffolding_children", return_value=[]):
            poll_cb()

        # Second assign without provider override (role gemini defaults to gemini provider)
        new_pane = _make_working_pane(cwd=str(tmp_path))
        new_pane.session = None
        new_pane.state = "empty"
        orch.paneRequested.connect(
            lambda role, project, o=orch, p=new_pane: o._panes_by_project.setdefault(
                project, {}
            ).__setitem__(role, p)
        )

        captured_argv: list[list[str]] = []
        fake_sess = MagicMock()
        fake_sess.processExited = MagicMock()
        fake_sess.processExited.connect = MagicMock()

        with (
            patch("agent_takkub.spawn_engine._cwd_within_project", return_value=True),
            patch.object(orch_mod.PtySession, "__new__", return_value=fake_sess),
            patch.object(
                fake_sess,
                "spawn",
                side_effect=lambda argv, cwd, env, **kwargs: captured_argv.append(list(argv)),
            ),
            patch.object(orch, "_send_when_ready"),
        ):
            ok, msg = orch.assign(
                "gemini",
                cwd=str(tmp_path),
                task="what was the word?",
                project=TEST_PROJECT,
            )
            assert ok, msg
            assert captured_argv
            argv = captured_argv[0]
            assert "--resume" not in argv
            assert self.UUID not in argv

    def test_grace_expired_deferred_close_preserves_resume(
        self, orch: Orchestrator, tmp_path
    ) -> None:
        key = _exit_key(TEST_PROJECT, "backend")
        pane = _make_working_pane(cwd=str(tmp_path))
        pane.model = MagicMock()
        pane.model.provider_name = "claude"
        pane.model.session_uuid = self.UUID
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane
        orch._panes_by_project[TEST_PROJECT]["lead"] = _make_lead_pane()

        ps = orch._ps(key)
        ps.session_uuid = self.UUID
        ps.session_uuid_cwd = str(tmp_path)
        ps.session_provider = "claude"
        ps.last_assigned_task = "task-grace"
        ps.task_id = "task-2"
        ps.task_delivered = True

        scheduled: list = []
        with patch(
            "agent_takkub.orchestrator.QTimer.singleShot",
            side_effect=lambda ms, cb: scheduled.append((ms, cb)),
        ):
            orch.done("backend", note="done", project=TEST_PROJECT)

        close_cb = next(cb for ms, cb in scheduled if ms == 2500)

        # Fast-forward time past grace period
        clock = {"t": 1000.0}
        with (
            patch("agent_takkub.orchestrator.time.time", side_effect=lambda: clock["t"]),
            patch.object(orch, "_live_non_scaffolding_children", return_value=["hung_child.exe"]),
        ):
            poll_timers: list = []
            with patch(
                "agent_takkub.orchestrator.QTimer.singleShot",
                side_effect=lambda ms, cb: poll_timers.append((ms, cb)),
            ):
                close_cb()

            poll_cb = next(cb for ms, cb in poll_timers if ms == DONE_CLOSE_LIVE_CHILD_POLL_MS)
            clock["t"] += DONE_CLOSE_LIVE_CHILD_GRACE_S + 10.0
            poll_cb()

        # Verify close on grace expiration preserved resume
        seeded = orch._pane_state.get(key)
        assert seeded is not None
        assert seeded.session_uuid == self.UUID
        assert seeded.session_provider == "claude"
        exit_rec = orch._recent_exits.get(key)
        assert exit_rec is not None
        assert exit_rec["provider"] == "claude"
