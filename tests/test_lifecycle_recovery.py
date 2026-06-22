"""Unit tests for the lifecycle/recovery cluster fixes (bugs 1-8 from gap audit).

Covers:
  Bug 1 — stuck-recover preserves session UUID, task, auto-chain, requires-commit
  Bug 2 — spinner-only bytes don't prevent stuck detection; real content change does
  Bug 3 — respawn cap warns Lead and clears auto-chain/task
  Bug 4 — manual spawn resets respawn counter; auto-respawn does not
  Bug 5 — resumed session skips task replay in _auto_respawn
  Bug 6 — close() pops harvest_hint_ts, last_stuck_recover, rate_limited_until
  Bug 7 — PtySession.terminate() calls quit()+wait() on reader/writer threads
  Bug 8 — codex/gemini roles use Opus/high tier
  Fix 1 — re-paste gate uses structured _last_spawn_resumed flag, not string parse
  Fix 2 — _do_respawn rolls back restored state when spawn() fails
  Fix 3 — spinner filter covers volatile counter lines without interrupt phrase
  m3    — _do_respawn synthesises _recent_exits entry to avoid PTY-teardown race
"""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import (
    _STUCK_RESUME_NUDGE,
    AUTO_RESPAWN_MAX,
    STUCK_THRESHOLD_S,
    Orchestrator,
    PaneState,
    _exit_key,
)

# Hash produced by the blake2b filter when ALL visible lines are spinner
# lines (filtered out → empty string). Pre-computed once so tests that
# pre-seed last_content_hash stay in sync with the orchestrator's hash algo.
_EMPTY_FILTERED_HASH = hashlib.blake2b(b"", digest_size=8).hexdigest()

TEST_PROJECT = "testproj"
SAMPLE_TASK = "[ROLE: backend] implement /auth/logout\ntakkub done when done"


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
    o._idle_watchdog.stop()
    return o


def _working_pane(cwd: str = "/proj") -> MagicMock:
    pane = MagicMock()
    pane.state = "working"
    pane._session_cwd = cwd
    pane._last_output_ts = 1_000_000.0
    sess = MagicMock()
    sess.is_alive = True
    sess.display_lines.return_value = ["line1", "line2", "ready"]
    pane.session = sess
    return pane


# ─────────────────────────────────────────────────────────────
# Bug 1: stuck-recover preserves per-pane state
# ─────────────────────────────────────────────────────────────


class TestStuckRecoverPreservesState:
    """_auto_recover_stuck must snapshot state before close() and restore it
    so spawn() can use --resume and the task/flags survive the recovery."""

    def _setup(self, orch: Orchestrator, key: str) -> MagicMock:
        pane = _working_pane()
        project, role = key.split("::", 1)
        orch._panes_by_project.setdefault(project, {})[role] = pane
        ps = orch._ps(key)
        ps.session_uuid = "test-uuid-1234"
        ps.session_uuid_cwd = "/proj"
        ps.last_assigned_task = SAMPLE_TASK
        ps.auto_chain = True
        ps.requires_commit_on_done = True
        return pane

    def test_session_uuid_restored_after_recover(self, orch: Orchestrator) -> None:
        key = _exit_key(TEST_PROJECT, "backend")
        pane = self._setup(orch, key)
        now = 1_000_000.0

        with (
            patch("agent_takkub.orchestrator.QTimer") as mock_timer,
            patch.object(orch, "spawn", return_value=(True, "backend spawned")),
        ):
            # Fire singleShot callbacks inline
            mock_timer.singleShot.side_effect = lambda ms, fn: fn()
            orch._auto_recover_stuck("backend", TEST_PROJECT, pane, now)

        assert orch._pane_state.get(key) is not None
        assert orch._pane_state[key].session_uuid == "test-uuid-1234"

    def test_last_task_restored_after_recover(self, orch: Orchestrator) -> None:
        key = _exit_key(TEST_PROJECT, "qa")
        pane = self._setup(orch, key)
        now = 1_000_000.0

        with (
            patch("agent_takkub.orchestrator.QTimer") as mock_timer,
            patch.object(orch, "spawn", return_value=(True, "qa spawned")),
            patch.object(orch, "_send_when_ready"),
        ):
            mock_timer.singleShot.side_effect = lambda ms, fn: fn()
            orch._auto_recover_stuck("qa", TEST_PROJECT, pane, now)

        assert (orch._pane_state.get(key) or PaneState()).last_assigned_task == SAMPLE_TASK

    def test_auto_chain_restored_after_recover(self, orch: Orchestrator) -> None:
        key = _exit_key(TEST_PROJECT, "frontend")
        pane = self._setup(orch, key)
        now = 1_000_000.0

        with (
            patch("agent_takkub.orchestrator.QTimer") as mock_timer,
            patch.object(orch, "spawn", return_value=(True, "frontend spawned")),
            patch.object(orch, "_send_when_ready"),
        ):
            mock_timer.singleShot.side_effect = lambda ms, fn: fn()
            orch._auto_recover_stuck("frontend", TEST_PROJECT, pane, now)

        assert (orch._pane_state.get(key) or PaneState()).auto_chain is True

    def test_requires_commit_restored_after_recover(self, orch: Orchestrator) -> None:
        key = _exit_key(TEST_PROJECT, "devops")
        pane = self._setup(orch, key)
        now = 1_000_000.0

        with (
            patch("agent_takkub.orchestrator.QTimer") as mock_timer,
            patch.object(orch, "spawn", return_value=(True, "devops spawned")),
            patch.object(orch, "_send_when_ready"),
        ):
            mock_timer.singleShot.side_effect = lambda ms, fn: fn()
            orch._auto_recover_stuck("devops", TEST_PROJECT, pane, now)

        assert (orch._pane_state.get(key) or PaneState()).requires_commit_on_done is True

    def test_spawn_called_with_from_auto_respawn_true(self, orch: Orchestrator) -> None:
        key = _exit_key(TEST_PROJECT, "mobile")
        pane = self._setup(orch, key)
        now = 1_000_000.0

        with (
            patch("agent_takkub.orchestrator.QTimer") as mock_timer,
            patch.object(orch, "spawn", return_value=(True, "mobile spawned")) as mock_spawn,
            patch.object(orch, "_send_when_ready"),
        ):
            mock_timer.singleShot.side_effect = lambda ms, fn: fn()
            orch._auto_recover_stuck("mobile", TEST_PROJECT, pane, now)

        assert mock_spawn.call_args.kwargs.get("_from_auto_respawn") is True

    def test_nudge_not_full_task_when_resumed(self, orch: Orchestrator) -> None:
        key = _exit_key(TEST_PROJECT, "reviewer")
        pane = self._setup(orch, key)
        now = 1_000_000.0

        def _spawn_resumed(*_a, **_kw):
            orch._ps(key).last_spawn_resumed = True
            return (True, "reviewer spawned (resumed)")

        with (
            patch("agent_takkub.orchestrator.QTimer") as mock_timer,
            patch.object(orch, "spawn", side_effect=_spawn_resumed),
            patch.object(orch, "_send_when_ready") as mock_send,
        ):
            mock_timer.singleShot.side_effect = lambda ms, fn: fn()
            orch._auto_recover_stuck("reviewer", TEST_PROJECT, pane, now)

        # Resumed pane: send a short continue-nudge (claude does not auto-continue
        # an interrupted turn), NOT the full task — the task is already in the
        # restored conversation history (Bug-5 gate against double-work).
        mock_send.assert_called_once_with("reviewer", _STUCK_RESUME_NUDGE, project=TEST_PROJECT)

    def test_task_replayed_when_fresh_spawn(self, orch: Orchestrator) -> None:
        key = _exit_key(TEST_PROJECT, "critic")
        pane = self._setup(orch, key)
        now = 1_000_000.0

        def _spawn_fresh(*_a, **_kw):
            orch._ps(key).last_spawn_resumed = False
            return (True, "critic spawned in /proj")

        with (
            patch("agent_takkub.orchestrator.QTimer") as mock_timer,
            patch.object(orch, "spawn", side_effect=_spawn_fresh),
            patch.object(orch, "_send_when_ready") as mock_send,
        ):
            mock_timer.singleShot.side_effect = lambda ms, fn: fn()
            orch._auto_recover_stuck("critic", TEST_PROJECT, pane, now)

        mock_send.assert_called_once_with("critic", SAMPLE_TASK, project=TEST_PROJECT)


# ─────────────────────────────────────────────────────────────
# Bug 2: spinner bytes don't prevent stuck detection
# ─────────────────────────────────────────────────────────────


class _FakeOrchForContentDelta:
    """Minimal orchestrator stub for testing _check_stuck_panes content-delta logic."""

    def __init__(self) -> None:
        self._panes_by_project: dict[str, dict] = {}
        self._pane_state: dict[str, PaneState] = {}
        self._idle_state: dict[str, dict] = {}
        self._recent_exits: dict[str, dict] = {}
        self.recover_calls: list[tuple[str, str]] = []

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
        return True, "ok"

    def spawn(self, role: str, cwd=None, project=None, **_kw):
        return True, "ok"

    def _send_when_ready(self, *_a, **_kw) -> None:
        pass

    def _auto_recover_stuck(self, role, project, pane, now) -> None:
        self.recover_calls.append((role, project))
        Orchestrator._auto_recover_stuck(self, role, project, pane, now)  # type: ignore[arg-type]

    def _project_panes(self, project: str | None = None) -> dict:
        return self._panes_by_project.get(project or "", {})

    def _surface_tty_block_notice(self, role, project, prompt_line) -> None:
        pass  # no-op stub — TTY-block tests live in test_stuck_recover.py

    def _maybe_surface_tty_block(self, key, role, project, prompt_line, now) -> None:
        pass  # no-op stub


def _check_stuck(fake, now: float) -> None:

    Orchestrator._check_stuck_panes(fake, now)  # type: ignore[arg-type]


class TestSpinnerBlindspotsFixed:
    """Content-delta stuck detection: spinner-only bytes must not reset the clock."""

    def test_spinner_only_bytes_still_detect_stuck(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pane that only outputs spinner lines should still trip stuck threshold.

        The non-spinner filtered hash for a pane whose only lines contain
        'esc to interrupt' is blake2b("") (empty string after filtering) —
        that stays constant across ticks while bytes keep arriving, so
        _last_content_change_ts never updates and the pane is eventually
        detected as stuck."""
        fired: list = []

        class _ShotCapture:
            @staticmethod
            def singleShot(ms, fn):
                fired.append(fn)
                fn()

        monkeypatch.setattr("agent_takkub.orchestrator.QTimer", _ShotCapture)

        fake = _FakeOrchForContentDelta()
        now = 2_000_000.0
        SPINNER_LINE = "⠋ 5:12  esc to interrupt"

        pane = MagicMock()
        pane.state = "working"
        pane._session_cwd = "/proj"
        # Raw bytes arrived recently (spinner is active), but content is stale
        pane._last_output_ts = now - 1
        sess = MagicMock()
        sess.is_alive = True
        sess.is_blocked_on_tty_prompt.return_value = None
        sess.is_at_update_splash.return_value = False
        # Only spinner lines — non-spinner hash = blake2b("") on every tick
        sess.display_lines.return_value = [SPINNER_LINE, SPINNER_LINE]
        pane.session = sess

        fake._panes_by_project["p"] = {"backend": pane}

        key = "p::backend"
        # Pre-seed the content hash as the FILTERED hash (empty — spinner lines
        # are excluded so the stored hash is blake2b(""), not hash of spinner text).
        fake._ps(key).last_content_hash = _EMPTY_FILTERED_HASH
        # Content last changed > threshold ago
        fake._ps(key).last_content_change_ts = now - STUCK_THRESHOLD_S - 5

        _check_stuck(fake, now)
        assert fake.recover_calls == [("backend", "p")]

    def test_real_content_change_resets_stuck_clock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When non-spinner content changes the watchdog must NOT recover the pane."""

        class _ShotCapture:
            @staticmethod
            def singleShot(ms, fn):
                fn()

        monkeypatch.setattr("agent_takkub.orchestrator.QTimer", _ShotCapture)

        fake = _FakeOrchForContentDelta()
        now = 2_000_000.0

        pane = MagicMock()
        pane.state = "working"
        pane._session_cwd = "/proj"
        pane._last_output_ts = now - STUCK_THRESHOLD_S - 5  # stale raw ts
        sess = MagicMock()
        sess.is_alive = True
        # New real content (no spinner) — pane is active
        sess.display_lines.return_value = [">>> new result here", "done"]
        pane.session = sess

        fake._panes_by_project["p"] = {"frontend": pane}

        key = "p::frontend"
        # Set previous hash to something DIFFERENT so change is detected
        fake._ps(key).last_content_hash = "old-hash-value"
        # Content change ts is 10s ago — content changed recently → not stuck
        fake._ps(key).last_content_change_ts = now - (STUCK_THRESHOLD_S - 10)

        _check_stuck(fake, now)
        assert fake.recover_calls == []

    def test_first_tick_initialises_from_last_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """On first observation, _last_content_change_ts must be seeded from last_out
        (not from now), so a pane that's been stale since before the first watchdog
        tick is detected immediately."""

        class _ShotCapture:
            @staticmethod
            def singleShot(ms, fn):
                fn()

        monkeypatch.setattr("agent_takkub.orchestrator.QTimer", _ShotCapture)

        fake = _FakeOrchForContentDelta()
        now = 2_000_000.0
        stale_out = now - STUCK_THRESHOLD_S - 1

        pane = MagicMock()
        pane.state = "working"
        pane._session_cwd = "/proj"
        pane._last_output_ts = stale_out
        sess = MagicMock()
        sess.is_alive = True
        sess.is_blocked_on_tty_prompt.return_value = None
        sess.is_at_update_splash.return_value = False
        sess.display_lines.return_value = ["some content"]
        pane.session = sess

        fake._panes_by_project["p"] = {"qa": pane}
        # No prior hash → first tick

        _check_stuck(fake, now)
        assert fake.recover_calls == [("qa", "p")]


# ─────────────────────────────────────────────────────────────
# Bug 3: respawn cap warns Lead and clears stale state
# ─────────────────────────────────────────────────────────────


class TestRespawnCapWarnsLead:
    def test_cap_warns_lead_when_alive(self, orch: Orchestrator) -> None:
        key = _exit_key(TEST_PROJECT, "backend")
        orch._ps(key).auto_respawn_attempts = AUTO_RESPAWN_MAX

        lead_pane = MagicMock()
        lead_pane.session = MagicMock()
        lead_pane.session.is_alive = True
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["lead"] = lead_pane

        pane = MagicMock()
        pane.state = "exited"
        pane.session = None
        orch._panes_by_project[TEST_PROJECT]["backend"] = pane

        with patch("agent_takkub.orchestrator.QTimer"):
            orch._on_session_exit("backend", "/proj", TEST_PROJECT)

        lead_pane.session.write.assert_called_once()
        written = lead_pane.session.write.call_args.args[0]
        assert "respawn-capped" in written
        assert "backend" in written

    def test_cap_clears_auto_chain(self, orch: Orchestrator) -> None:
        key = _exit_key(TEST_PROJECT, "frontend")
        orch._ps(key).auto_respawn_attempts = AUTO_RESPAWN_MAX
        orch._ps(key).auto_chain = True

        pane = MagicMock()
        pane.state = "exited"
        pane.session = None
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["frontend"] = pane

        with patch("agent_takkub.orchestrator.QTimer"):
            orch._on_session_exit("frontend", "/proj", TEST_PROJECT)

        assert not (orch._pane_state.get(key) or PaneState()).auto_chain

    def test_cap_clears_last_assigned_task(self, orch: Orchestrator) -> None:
        key = _exit_key(TEST_PROJECT, "devops")
        orch._ps(key).auto_respawn_attempts = AUTO_RESPAWN_MAX
        orch._ps(key).last_assigned_task = SAMPLE_TASK

        pane = MagicMock()
        pane.state = "exited"
        pane.session = None
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["devops"] = pane

        with patch("agent_takkub.orchestrator.QTimer"):
            orch._on_session_exit("devops", "/proj", TEST_PROJECT)

        assert (orch._pane_state.get(key) or PaneState()).last_assigned_task is None


# ─────────────────────────────────────────────────────────────
# Bug 4: AUTO_RESPAWN_MAX counter resets on manual spawn
# ─────────────────────────────────────────────────────────────


class TestRespawnCounterReset:
    def _register_pane(self, orch: Orchestrator, role: str) -> MagicMock:
        pane = MagicMock()
        pane.session = None
        pane.state = "empty"
        pane.attach_session = MagicMock()
        pane._transcript_path = None
        orch._panes_by_project.setdefault(TEST_PROJECT, {})[role] = pane
        return pane

    def test_manual_spawn_resets_counter(self, orch: Orchestrator) -> None:
        key = _exit_key(TEST_PROJECT, "backend")
        orch._ps(key).auto_respawn_attempts = AUTO_RESPAWN_MAX - 1

        self._register_pane(orch, "backend")
        fake_session = MagicMock()
        fake_session.processExited = MagicMock()
        fake_session.processExited.connect = MagicMock()

        import agent_takkub.orchestrator as orch_mod

        with patch.object(orch_mod.PtySession, "__new__", return_value=fake_session):
            with patch.object(fake_session, "spawn"):
                orch.spawn("backend", cwd="/proj", project=TEST_PROJECT)

        assert (orch._pane_state.get(key) or PaneState()).auto_respawn_attempts == 0

    def test_auto_respawn_does_not_reset_counter(self, orch: Orchestrator) -> None:
        key = _exit_key(TEST_PROJECT, "qa")
        orch._ps(key).auto_respawn_attempts = 2

        self._register_pane(orch, "qa")
        fake_session = MagicMock()
        fake_session.processExited = MagicMock()
        fake_session.processExited.connect = MagicMock()

        import agent_takkub.orchestrator as orch_mod

        with patch.object(orch_mod.PtySession, "__new__", return_value=fake_session):
            with patch.object(fake_session, "spawn"):
                orch.spawn("qa", cwd="/proj", project=TEST_PROJECT, _from_auto_respawn=True)

        # Counter must NOT be cleared
        assert (orch._pane_state.get(key) or PaneState()).auto_respawn_attempts == 2


# ─────────────────────────────────────────────────────────────
# Bug 5: no task replay on resumed session in _auto_respawn
# ─────────────────────────────────────────────────────────────


class TestNoReplayOnResumedSession:
    def test_no_replay_when_spawn_returns_resumed(self, orch: Orchestrator) -> None:
        key = _exit_key(TEST_PROJECT, "mobile")
        orch._ps(key).last_assigned_task = SAMPLE_TASK

        pane = MagicMock()
        pane.session = None
        pane.state = "exited"
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["mobile"] = pane

        def _spawn_resumed(*_a, **_kw):
            orch._ps(key).last_spawn_resumed = True
            return (True, "mobile spawned (resumed)")

        with (
            patch.object(orch, "spawn", side_effect=_spawn_resumed),
            patch.object(orch, "_send_when_ready") as mock_send,
        ):
            orch._auto_respawn("mobile", "/proj", TEST_PROJECT)

        mock_send.assert_not_called()

    def test_replay_when_spawn_is_fresh(self, orch: Orchestrator) -> None:
        key = _exit_key(TEST_PROJECT, "mobile")
        orch._ps(key).last_assigned_task = SAMPLE_TASK

        pane = MagicMock()
        pane.session = None
        pane.state = "exited"
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["mobile"] = pane

        def _spawn_fresh(*_a, **_kw):
            orch._ps(key).last_spawn_resumed = False
            return (True, "mobile spawned in /proj")

        with (
            patch.object(orch, "spawn", side_effect=_spawn_fresh),
            patch.object(orch, "_send_when_ready") as mock_send,
        ):
            orch._auto_respawn("mobile", "/proj", TEST_PROJECT)

        mock_send.assert_called_once_with("mobile", SAMPLE_TASK, project=TEST_PROJECT)


# ─────────────────────────────────────────────────────────────
# Bug 6: close() pops previously-leaking state dicts
# ─────────────────────────────────────────────────────────────


class TestCloseLeakFix:
    def _make_alive_pane(self) -> MagicMock:
        pane = MagicMock()
        pane.session = MagicMock()
        pane.session.is_alive = True
        pane.state = "working"
        pane.mark_expected_exit = MagicMock()
        pane.session.terminate = MagicMock()
        pane.set_state = MagicMock()
        return pane

    def test_harvest_hint_ts_popped(self, orch: Orchestrator) -> None:
        key = _exit_key(TEST_PROJECT, "qa")
        orch._ps(key).harvest_hint_ts = 12345.0
        pane = self._make_alive_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["qa"] = pane

        orch.close("qa", project=TEST_PROJECT)

        assert orch._pane_state.get(key) is None

    def test_last_stuck_recover_popped(self, orch: Orchestrator) -> None:
        key = _exit_key(TEST_PROJECT, "frontend")
        orch._ps(key).last_stuck_recover = 99999.0
        pane = self._make_alive_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["frontend"] = pane

        orch.close("frontend", project=TEST_PROJECT)

        assert orch._pane_state.get(key) is None

    def test_rate_limited_until_popped(self, orch: Orchestrator) -> None:
        key = _exit_key(TEST_PROJECT, "backend")
        orch._ps(key).rate_limited_until = 99999.0
        pane = self._make_alive_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane

        orch.close("backend", project=TEST_PROJECT)

        assert orch._pane_state.get(key) is None

    def test_content_hash_popped(self, orch: Orchestrator) -> None:
        key = _exit_key(TEST_PROJECT, "reviewer")
        orch._ps(key).last_content_hash = "abc"
        orch._ps(key).last_content_change_ts = 1234.0
        pane = self._make_alive_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["reviewer"] = pane

        orch.close("reviewer", project=TEST_PROJECT)

        assert orch._pane_state.get(key) is None


# ─────────────────────────────────────────────────────────────
# Bug 7: PtySession.terminate() joins threads
# ─────────────────────────────────────────────────────────────


class TestPtySessionTerminateJoinsThreads:
    def test_terminate_calls_quit_and_wait_on_writer(self) -> None:
        from agent_takkub.pty_session import PtySession

        session = PtySession()
        writer = MagicMock()
        reader = MagicMock()
        session._writer = writer
        session._reader = reader
        session._proc = None
        session._alive = True
        session._transcript = None

        session.terminate()

        writer.request_stop.assert_called_once()
        writer.quit.assert_called_once()
        writer.wait.assert_called_once_with(500)

    def test_terminate_calls_quit_and_wait_on_reader(self) -> None:
        from agent_takkub.pty_session import PtySession

        session = PtySession()
        writer = MagicMock()
        reader = MagicMock()
        session._writer = writer
        session._reader = reader
        session._proc = None
        session._alive = True
        session._transcript = None

        session.terminate()

        reader.request_stop.assert_called_once()
        reader.quit.assert_called_once()
        reader.wait.assert_called_once_with(500)

    def test_terminate_terminates_proc_before_join(self) -> None:
        """proc must be killed before wait() so the reader's blocking read() unblocks."""
        from agent_takkub.pty_session import PtySession

        session = PtySession()
        call_order: list[str] = []
        writer = MagicMock()
        reader = MagicMock()
        proc = MagicMock()
        proc.terminate = MagicMock(side_effect=lambda **_kw: call_order.append("proc_term"))
        writer.quit = MagicMock(side_effect=lambda: call_order.append("writer_quit"))

        session._writer = writer
        session._reader = reader
        session._proc = proc
        session._alive = True
        session._transcript = None

        session.terminate()

        assert call_order.index("proc_term") < call_order.index("writer_quit")


# ─────────────────────────────────────────────────────────────
# Bug 8: codex/gemini roles use Opus/high tier
# ─────────────────────────────────────────────────────────────


class TestCodexGeminiModelTier:
    def test_codex_uses_opus_high(self) -> None:
        from agent_takkub.orchestrator import _teammate_tier

        model, effort, fallback = _teammate_tier("codex")
        assert model == "claude-opus-4-8"
        assert effort == "high"
        assert fallback == "claude-sonnet-4-6"

    def test_gemini_uses_opus_high(self) -> None:
        from agent_takkub.orchestrator import _teammate_tier

        model, effort, fallback = _teammate_tier("gemini")
        assert model == "claude-opus-4-8"
        assert effort == "high"
        assert fallback == "claude-sonnet-4-6"

    def test_codex_gemini_tier_higher_than_default(self) -> None:
        from agent_takkub.orchestrator import _DEFAULT_TEAMMATE_TIER, _teammate_tier

        for role in ("codex", "gemini"):
            assert _teammate_tier(role) != _DEFAULT_TEAMMATE_TIER, (
                f"{role} must not fall to default Sonnet/medium tier"
            )


# ─────────────────────────────────────────────────────────────
# Fix 1: structured _last_spawn_resumed flag (string-coupling fix)
# ─────────────────────────────────────────────────────────────


class TestStructuredResumeFlag:
    """_auto_respawn and _do_respawn must read _last_spawn_resumed, not parse msg."""

    def _make_crashed_pane(self, orch: Orchestrator, role: str) -> None:
        pane = MagicMock()
        pane.session = None
        pane.state = "exited"
        orch._panes_by_project.setdefault(TEST_PROJECT, {})[role] = pane

    def test_cwd_with_resumed_substring_still_replays(self, orch: Orchestrator) -> None:
        """A cwd like '/work/(resumed)-migration' must NOT suppress replay."""
        key = _exit_key(TEST_PROJECT, "backend")
        orch._ps(key).last_assigned_task = SAMPLE_TASK
        self._make_crashed_pane(orch, "backend")

        def _spawn_fresh_tricky(*_a, **_kw):
            # spawn sets flag=False (fresh), but message contains "(resumed)"
            orch._ps(key).last_spawn_resumed = False
            return (True, "backend spawned in /work/(resumed)-migration")

        with (
            patch.object(orch, "spawn", side_effect=_spawn_fresh_tricky),
            patch.object(orch, "_send_when_ready") as mock_send,
        ):
            orch._auto_respawn("backend", "/work/(resumed)-migration", TEST_PROJECT)

        mock_send.assert_called_once_with("backend", SAMPLE_TASK, project=TEST_PROJECT)

    def test_close_pops_last_spawn_resumed(self, orch: Orchestrator) -> None:
        key = _exit_key(TEST_PROJECT, "qa")
        orch._ps(key).last_spawn_resumed = True

        pane = MagicMock()
        pane.session = MagicMock()
        pane.session.is_alive = True
        pane.state = "working"
        pane.mark_expected_exit = MagicMock()
        pane.session.terminate = MagicMock()
        pane.set_state = MagicMock()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["qa"] = pane

        orch.close("qa", project=TEST_PROJECT)

        assert orch._pane_state.get(key) is None

    def test_auto_respawn_fresh_spawn_no_flag_means_replay(self, orch: Orchestrator) -> None:
        """If last_spawn_resumed is False (default), task is replayed."""
        key = _exit_key(TEST_PROJECT, "devops")
        orch._ps(key).last_assigned_task = SAMPLE_TASK
        self._make_crashed_pane(orch, "devops")
        # Do NOT set _last_spawn_resumed — absence should be treated as fresh

        with (
            patch.object(orch, "spawn", return_value=(True, "devops spawned")),
            patch.object(orch, "_send_when_ready") as mock_send,
        ):
            orch._auto_respawn("devops", "/proj", TEST_PROJECT)

        mock_send.assert_called_once_with("devops", SAMPLE_TASK, project=TEST_PROJECT)


# ─────────────────────────────────────────────────────────────
# Fix 2: _do_respawn rolls back state on spawn failure
# ─────────────────────────────────────────────────────────────


class TestDoRespawnRollbackOnFailure:
    """When spawn() returns False in _do_respawn, restored state must be rolled back."""

    def _setup(self, orch: Orchestrator, key: str) -> MagicMock:
        pane = _working_pane()
        project, role = key.split("::", 1)
        orch._panes_by_project.setdefault(project, {})[role] = pane
        ps = orch._ps(key)
        ps.session_uuid = "test-uuid-rollback"
        ps.session_uuid_cwd = "/proj"
        ps.last_assigned_task = SAMPLE_TASK
        ps.auto_chain = True
        ps.requires_commit_on_done = True
        return pane

    def test_uuid_rolled_back_on_spawn_failure(self, orch: Orchestrator) -> None:
        key = _exit_key(TEST_PROJECT, "frontend")
        pane = self._setup(orch, key)
        now = 1_000_000.0

        with (
            patch("agent_takkub.orchestrator.QTimer") as mock_timer,
            patch.object(orch, "spawn", return_value=(False, "spawn failed: cwd not found")),
        ):
            mock_timer.singleShot.side_effect = lambda ms, fn: fn()
            orch._auto_recover_stuck("frontend", TEST_PROJECT, pane, now)

        assert (orch._pane_state.get(key) or PaneState()).session_uuid is None

    def test_task_rolled_back_on_spawn_failure(self, orch: Orchestrator) -> None:
        key = _exit_key(TEST_PROJECT, "mobile")
        pane = self._setup(orch, key)
        now = 1_000_000.0

        with (
            patch("agent_takkub.orchestrator.QTimer") as mock_timer,
            patch.object(orch, "spawn", return_value=(False, "spawn failed")),
        ):
            mock_timer.singleShot.side_effect = lambda ms, fn: fn()
            orch._auto_recover_stuck("mobile", TEST_PROJECT, pane, now)

        assert (orch._pane_state.get(key) or PaneState()).last_assigned_task is None

    def test_auto_chain_rolled_back_on_spawn_failure(self, orch: Orchestrator) -> None:
        key = _exit_key(TEST_PROJECT, "reviewer")
        pane = self._setup(orch, key)
        now = 1_000_000.0

        with (
            patch("agent_takkub.orchestrator.QTimer") as mock_timer,
            patch.object(orch, "spawn", return_value=(False, "spawn failed")),
        ):
            mock_timer.singleShot.side_effect = lambda ms, fn: fn()
            orch._auto_recover_stuck("reviewer", TEST_PROJECT, pane, now)

        assert not (orch._pane_state.get(key) or PaneState()).auto_chain

    def test_requires_commit_rolled_back_on_spawn_failure(self, orch: Orchestrator) -> None:
        key = _exit_key(TEST_PROJECT, "devops")
        pane = self._setup(orch, key)
        now = 1_000_000.0

        with (
            patch("agent_takkub.orchestrator.QTimer") as mock_timer,
            patch.object(orch, "spawn", return_value=(False, "spawn failed")),
        ):
            mock_timer.singleShot.side_effect = lambda ms, fn: fn()
            orch._auto_recover_stuck("devops", TEST_PROJECT, pane, now)

        assert not (orch._pane_state.get(key) or PaneState()).requires_commit_on_done

    def test_pane_state_fully_popped_on_spawn_failure(self, orch: Orchestrator) -> None:
        """Regression: rollback must pop the whole PaneState, not just reset fields.
        Field-reset left an empty PaneState entry that weakens the 'popped atomically
        by close()/done()' contract and can skew future membership checks."""
        key = _exit_key(TEST_PROJECT, "backend")
        pane = self._setup(orch, key)
        now = 1_000_000.0

        with (
            patch("agent_takkub.orchestrator.QTimer") as mock_timer,
            patch.object(orch, "spawn", return_value=(False, "spawn failed: cwd not found")),
        ):
            mock_timer.singleShot.side_effect = lambda ms, fn: fn()
            orch._auto_recover_stuck("backend", TEST_PROJECT, pane, now)

        assert orch._pane_state.get(key) is None, (
            "failed rollback must pop the whole PaneState entry (not reset fields), "
            "matching the 'popped atomically by close()/done()' contract"
        )

    def test_no_send_when_ready_after_spawn_failure(self, orch: Orchestrator) -> None:
        key = _exit_key(TEST_PROJECT, "qa")
        pane = self._setup(orch, key)
        now = 1_000_000.0

        with (
            patch("agent_takkub.orchestrator.QTimer") as mock_timer,
            patch.object(orch, "spawn", return_value=(False, "spawn failed")),
            patch.object(orch, "_send_when_ready") as mock_send,
        ):
            mock_timer.singleShot.side_effect = lambda ms, fn: fn()
            orch._auto_recover_stuck("qa", TEST_PROJECT, pane, now)

        mock_send.assert_not_called()


# ─────────────────────────────────────────────────────────────
# Fix 3: spinner filter covers volatile counter lines
# ─────────────────────────────────────────────────────────────


class TestSpinnerFilterRobust:
    """Content-delta filter must exclude counter lines that lack 'esc to interrupt'."""

    def test_counter_line_without_interrupt_phrase_excluded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A line like '· 45s · ↑ 2.3k tokens' changes every tick but carries no
        content signal — the stuck watchdog must still detect a stale pane."""

        class _ShotCapture:
            @staticmethod
            def singleShot(ms, fn):
                fn()

        monkeypatch.setattr("agent_takkub.orchestrator.QTimer", _ShotCapture)

        fake = _FakeOrchForContentDelta()
        now = 2_000_000.0

        pane = MagicMock()
        pane.state = "working"
        pane._session_cwd = "/proj"
        pane._last_output_ts = now - 1  # raw bytes recent (spinner active)
        sess = MagicMock()
        sess.is_alive = True
        sess.is_blocked_on_tty_prompt.return_value = None
        sess.is_at_update_splash.return_value = False
        # Counter line WITHOUT 'esc to interrupt' — would defeat old filter
        sess.display_lines.return_value = ["· 45s · ↑ 2.3k tokens", "· 45s · ↑ 2.3k tokens"]
        pane.session = sess

        fake._panes_by_project["p"] = {"backend": pane}

        key = "p::backend"
        # Pre-seed with the blake2b("") hash (what filter produces for these lines)
        fake._ps(key).last_content_hash = _EMPTY_FILTERED_HASH
        fake._ps(key).last_content_change_ts = now - STUCK_THRESHOLD_S - 5

        _check_stuck(fake, now)
        assert fake.recover_calls == [("backend", "p")]

    def test_esc_to_stop_phrase_excluded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """'esc to stop' (alternate CLI phrasing) must also be filtered."""

        class _ShotCapture:
            @staticmethod
            def singleShot(ms, fn):
                fn()

        monkeypatch.setattr("agent_takkub.orchestrator.QTimer", _ShotCapture)

        fake = _FakeOrchForContentDelta()
        now = 2_000_000.0

        pane = MagicMock()
        pane.state = "working"
        pane._session_cwd = "/proj"
        pane._last_output_ts = now - 1
        sess = MagicMock()
        sess.is_alive = True
        sess.is_blocked_on_tty_prompt.return_value = None
        sess.is_at_update_splash.return_value = False
        sess.display_lines.return_value = ["⠸ running  esc to stop"]
        pane.session = sess

        fake._panes_by_project["p"] = {"backend": pane}

        key = "p::backend"
        fake._ps(key).last_content_hash = _EMPTY_FILTERED_HASH
        fake._ps(key).last_content_change_ts = now - STUCK_THRESHOLD_S - 5

        _check_stuck(fake, now)
        assert fake.recover_calls == [("backend", "p")]

    def test_down_token_counter_excluded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Down-arrow token counter ('↓ 100 tokens') is also volatile."""

        class _ShotCapture:
            @staticmethod
            def singleShot(ms, fn):
                fn()

        monkeypatch.setattr("agent_takkub.orchestrator.QTimer", _ShotCapture)

        fake = _FakeOrchForContentDelta()
        now = 2_000_000.0

        pane = MagicMock()
        pane.state = "working"
        pane._session_cwd = "/proj"
        pane._last_output_ts = now - 1
        sess = MagicMock()
        sess.is_alive = True
        sess.is_blocked_on_tty_prompt.return_value = None
        sess.is_at_update_splash.return_value = False
        sess.display_lines.return_value = ["↓ 100 tokens  · 12s ·"]
        pane.session = sess

        fake._panes_by_project["p"] = {"backend": pane}

        key = "p::backend"
        fake._ps(key).last_content_hash = _EMPTY_FILTERED_HASH
        fake._ps(key).last_content_change_ts = now - STUCK_THRESHOLD_S - 5

        _check_stuck(fake, now)
        assert fake.recover_calls == [("backend", "p")]


# ─────────────────────────────────────────────────────────────
# m3: _do_respawn synthesises _recent_exits to avoid PTY-teardown race
# ─────────────────────────────────────────────────────────────


class TestDoRespawnSynthesisedRecentExit:
    """_do_respawn must not rely solely on _on_session_exit populating _recent_exits
    within the 2s singleShot window.  When snap_uuid is available and the key is
    absent, it synthesises the entry so spawn()'s can_resume is always True."""

    def test_recent_exits_synthesised_when_absent(self, orch: Orchestrator) -> None:
        key = _exit_key(TEST_PROJECT, "backend")
        pane = _working_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane
        ps = orch._ps(key)
        ps.session_uuid = "synth-uuid-9999"
        ps.session_uuid_cwd = "/proj"
        ps.last_assigned_task = SAMPLE_TASK
        now = 1_000_000.0

        # Capture the state of _recent_exits at the moment spawn() is called.
        recent_exits_at_spawn: list[dict] = []

        def _mock_spawn(role, cwd=None, project=None, **kw):
            recent_exits_at_spawn.append(dict(orch._recent_exits))
            orch._ps(key).last_spawn_resumed = False
            return (True, "backend spawned")

        with (
            patch("agent_takkub.orchestrator.QTimer") as mock_timer,
            patch.object(orch, "spawn", side_effect=_mock_spawn),
            patch.object(orch, "_send_when_ready"),
        ):
            mock_timer.singleShot.side_effect = lambda ms, fn: fn()
            # Do NOT pre-populate _recent_exits — simulate slow PTY teardown
            assert key not in orch._recent_exits
            orch._auto_recover_stuck("backend", TEST_PROJECT, pane, now)

        # spawn was called exactly once (recovery ran)
        assert len(recent_exits_at_spawn) == 1
        # The synthetic entry must have been present when spawn() was called
        assert key in recent_exits_at_spawn[0], (
            f"_recent_exits was not synthesised before spawn(): saw {recent_exits_at_spawn[0]}"
        )
        assert recent_exits_at_spawn[0][key]["cwd"] == "/proj"

    def test_existing_recent_exit_not_overwritten(self, orch: Orchestrator) -> None:
        """If _recent_exits already has the entry (normal case), don't overwrite it."""
        key = _exit_key(TEST_PROJECT, "qa")
        pane = _working_pane()
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["qa"] = pane
        ps = orch._ps(key)
        ps.session_uuid = "existing-uuid"
        ps.session_uuid_cwd = "/proj"
        ps.last_assigned_task = SAMPLE_TASK
        now = 1_000_000.0
        original_ts = 999_999.0
        orch._recent_exits[key] = {"cwd": "/proj", "ts": original_ts}

        seen_recent_exit_ts: list[float] = []

        def _spy_spawn(role, cwd=None, project=None, **kw):
            ts = orch._recent_exits.get(key, {}).get("ts")
            if ts is not None:
                seen_recent_exit_ts.append(ts)
            orch._ps(key).last_spawn_resumed = False
            return (True, "qa spawned")

        with (
            patch("agent_takkub.orchestrator.QTimer") as mock_timer,
            patch.object(orch, "spawn", side_effect=_spy_spawn),
            patch.object(orch, "_send_when_ready"),
        ):
            mock_timer.singleShot.side_effect = lambda ms, fn: fn()
            orch._auto_recover_stuck("qa", TEST_PROJECT, pane, now)

        # The original ts must have been seen unchanged (not overwritten)
        assert seen_recent_exit_ts == [original_ts]
