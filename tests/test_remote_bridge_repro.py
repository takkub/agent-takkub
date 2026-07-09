"""Real-timing repro + regression tests for issue #4/#107 ("#4 auto
/remote-control did not fire on fresh Lead boot").

`test_remote_bridge_autofire.py` mocked `inject_slash_command_when_ready`
entirely out — it proves `_maybe_fire_remote_bridge` is *called* on every
Lead spawn, but never exercises the ready-prompt polling loop itself, which
is exactly where the real drop happened (fake-vs-real drift, per #107).
These tests drive the real `QTimer.singleShot` chain (captured + fired
manually, same technique as `test_lead_draft_guard.py`) with a fake session
whose `is_at_ready_prompt()` result is controlled per poll, so they prove:

  1. Every silent-drop exit path now logs `auto_slash_command_dropped` with
     a reason instead of vanishing with zero trace.
  2. A Lead pane that never reaches ready within the bridge's window is
     recovered by the idle-watchdog reaper (`_reap_remote_bridge`) instead
     of being lost for the rest of the session.
  3. `consume_session_report` (the `SessionStart` hook consumer) re-fires
     the bridge for a brand-new session-uuid — closing the gap where a
     manual `/resume` inside the Lead pane never got `/remote-control`
     re-established because `spawn()`'s own call only ever sees the uuid it
     stamped at spawn time.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import lead_inbox as lead_inbox_mod
from agent_takkub.orchestrator import Orchestrator
from agent_takkub.roles import LEAD

TEST_PROJECT = "bridgerepro"


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


def _make_session(*, ready_sequence) -> MagicMock:
    """A fake PtySession whose is_at_ready_prompt() returns the next value
    from *ready_sequence* on each call, repeating the last value once
    exhausted (so a long poll chain doesn't raise StopIteration)."""
    seq = list(ready_sequence)

    def _next_ready(*_a, **_kw):
        if seq:
            return seq.pop(0) if len(seq) > 1 else seq[0]
        return True

    s = MagicMock()
    s.is_alive = True
    s.is_at_ready_prompt = MagicMock(side_effect=_next_ready)
    s.write = MagicMock()
    return s


def _make_pane(*, ready_sequence) -> MagicMock:
    pane = MagicMock()
    pane.role.name = LEAD.name
    pane.session = _make_session(ready_sequence=ready_sequence)
    return pane


def _drive_timer_chain(capture_calls, *, max_iterations: int) -> None:
    """Manually fire captured QTimer.singleShot callbacks in order, the way
    a real event loop would over wall-clock time — without actually
    sleeping. Stops once nothing new gets scheduled or the iteration cap is
    hit (guards a test bug from spinning forever)."""
    i = 0
    while capture_calls and i < max_iterations:
        _delay, fn = capture_calls.pop(0)
        fn()
        i += 1


class TestInjectSlashCommandDropObservability:
    """Every exit path of inject_slash_command_when_ready must now log a
    reason instead of silently vanishing (#107)."""

    def test_pane_missing_drops_immediately_with_reason(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = []
        monkeypatch.setattr(lead_inbox_mod, "_log_event", lambda ev, **kw: events.append((ev, kw)))
        dropped = []

        orch.inject_slash_command_when_ready(
            LEAD.name, "/remote-control", project=TEST_PROJECT, on_dropped=dropped.append
        )

        assert dropped == ["pane_missing"]
        assert (
            "auto_slash_command_dropped",
            {"role": LEAD.name, "command": "/remote-control", "reason": "pane_missing"},
        ) in events

    def test_never_ready_within_window_drops_with_timeout_reason(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulates a fresh boot / boot-storm Lead that never paints its
        ready prompt inside the window — the literal #107 symptom. Under the
        pre-fix code this returned silently with zero log trace."""
        events = []
        monkeypatch.setattr(lead_inbox_mod, "_log_event", lambda ev, **kw: events.append((ev, kw)))
        pane = _make_pane(ready_sequence=[False])  # never becomes ready
        orch._panes_by_project.setdefault(TEST_PROJECT, {})[LEAD.name] = pane

        calls: list[tuple[int, object]] = []
        monkeypatch.setattr(
            lead_inbox_mod.QTimer, "singleShot", lambda ms, fn: calls.append((ms, fn))
        )
        dropped = []

        orch.inject_slash_command_when_ready(
            LEAD.name,
            "/remote-control",
            max_wait_ms=2_000,
            project=TEST_PROJECT,
            on_dropped=dropped.append,
        )
        _drive_timer_chain(calls, max_iterations=20)

        assert dropped == ["timeout_not_ready"]
        assert any(
            ev == "auto_slash_command_dropped" and kw.get("reason") == "timeout_not_ready"
            for ev, kw in events
        )
        pane.session.write.assert_not_called()

    def test_session_dies_mid_poll_drops_with_reason(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pane = _make_pane(ready_sequence=[False])
        orch._panes_by_project.setdefault(TEST_PROJECT, {})[LEAD.name] = pane

        calls: list[tuple[int, object]] = []
        monkeypatch.setattr(
            lead_inbox_mod.QTimer, "singleShot", lambda ms, fn: calls.append((ms, fn))
        )
        dropped = []

        orch.inject_slash_command_when_ready(
            LEAD.name,
            "/remote-control",
            max_wait_ms=30_000,
            project=TEST_PROJECT,
            on_dropped=dropped.append,
        )
        # First poll: still not ready, reschedules. Then the session dies
        # (e.g. crash / respawn mid-boot) before the next poll fires.
        _drive_timer_chain(calls, max_iterations=1)
        pane.session.is_alive = False
        _drive_timer_chain(calls, max_iterations=1)

        assert dropped == ["session_dead"]

    def test_becomes_ready_before_timeout_delivers(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pane = _make_pane(ready_sequence=[False, False, True])
        orch._panes_by_project.setdefault(TEST_PROJECT, {})[LEAD.name] = pane

        calls: list[tuple[int, object]] = []
        monkeypatch.setattr(
            lead_inbox_mod.QTimer, "singleShot", lambda ms, fn: calls.append((ms, fn))
        )
        delivered = []

        orch.inject_slash_command_when_ready(
            LEAD.name,
            "/remote-control",
            max_wait_ms=30_000,
            project=TEST_PROJECT,
            on_delivered=lambda: delivered.append(True),
        )
        _drive_timer_chain(calls, max_iterations=10)

        assert delivered == [True]
        # Paste + the delayed submitting Enter (_delayed_enter) both write to
        # the session — the paste itself is the first call.
        assert pane.session.write.call_args_list[0].args == ("/remote-control",)


class TestReapRemoteBridgeRetriesDrop:
    """A bridge that dropped (timeout/session-dead) must be retried by the
    idle-watchdog tick once the Lead pane is next observed ready — not lost
    for the rest of the session (#107)."""

    def test_dropped_bridge_retried_once_pane_ready(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch.inject_slash_command_when_ready = MagicMock()  # type: ignore[method-assign]
        pane = MagicMock()
        pane.session.is_alive = True
        pane.session.is_at_ready_prompt.return_value = True
        orch._panes_by_project.setdefault(TEST_PROJECT, {})[LEAD.name] = pane
        exit_key = f"{TEST_PROJECT}::{LEAD.name}"
        orch._ps(exit_key).session_uuid = "uuid-dropped"

        orch._maybe_fire_remote_bridge(TEST_PROJECT, exit_key)
        assert orch.inject_slash_command_when_ready.call_count == 1
        # Simulate the poll giving up (timeout) by invoking the on_dropped
        # callback _maybe_fire_remote_bridge passed in.
        _, first_kwargs = orch.inject_slash_command_when_ready.call_args
        first_kwargs["on_dropped"]("timeout_not_ready")

        key = f"{TEST_PROJECT}::uuid-dropped"
        assert key not in orch._lead_remote_bridge_fired
        assert key not in orch._lead_remote_bridge_pending

        orch._reap_remote_bridge(TEST_PROJECT, pane)

        assert orch.inject_slash_command_when_ready.call_count == 2

    def test_pending_bridge_not_double_polled_by_reaper(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch.inject_slash_command_when_ready = MagicMock()  # type: ignore[method-assign]
        pane = MagicMock()
        pane.session.is_alive = True
        pane.session.is_at_ready_prompt.return_value = True
        orch._panes_by_project.setdefault(TEST_PROJECT, {})[LEAD.name] = pane
        exit_key = f"{TEST_PROJECT}::{LEAD.name}"
        orch._ps(exit_key).session_uuid = "uuid-inflight"

        orch._maybe_fire_remote_bridge(TEST_PROJECT, exit_key)
        assert orch.inject_slash_command_when_ready.call_count == 1

        # Still polling (no on_dropped/on_delivered fired yet) — the reaper
        # must not start a second concurrent poll for the same session.
        orch._reap_remote_bridge(TEST_PROJECT, pane)

        assert orch.inject_slash_command_when_ready.call_count == 1

    def test_reaper_no_op_while_pane_not_ready(self, orch: Orchestrator) -> None:
        orch.inject_slash_command_when_ready = MagicMock()  # type: ignore[method-assign]
        pane = MagicMock()
        pane.session.is_alive = True
        pane.session.is_at_ready_prompt.return_value = False

        orch._reap_remote_bridge(TEST_PROJECT, pane)

        orch.inject_slash_command_when_ready.assert_not_called()


class TestSessionReportRefiresBridgeForNewSession:
    """#107 point 4: a manual /resume inside the Lead pane must re-fire the
    bridge for the new session-uuid — spawn()'s own call only ever sees the
    uuid it stamped at spawn time."""

    def test_resume_with_new_uuid_fires_bridge(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch.inject_slash_command_when_ready = MagicMock()  # type: ignore[method-assign]

        ok, _ = orch.consume_session_report(
            LEAD.name, project=TEST_PROJECT, session_id="uuid-boot", source="startup"
        )
        assert ok is True
        assert orch.inject_slash_command_when_ready.call_count == 1

        ok, _ = orch.consume_session_report(
            LEAD.name, project=TEST_PROJECT, session_id="uuid-resumed", source="resume"
        )
        assert ok is True
        assert orch.inject_slash_command_when_ready.call_count == 2

    def test_same_uuid_reported_twice_does_not_refire(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch.inject_slash_command_when_ready = MagicMock()  # type: ignore[method-assign]

        orch.consume_session_report(
            LEAD.name, project=TEST_PROJECT, session_id="uuid-same", source="startup"
        )
        orch.consume_session_report(
            LEAD.name, project=TEST_PROJECT, session_id="uuid-same", source="startup"
        )

        assert orch.inject_slash_command_when_ready.call_count == 1

    def test_non_lead_role_never_fires_bridge(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orch.inject_slash_command_when_ready = MagicMock()  # type: ignore[method-assign]

        orch.consume_session_report(
            "backend", project=TEST_PROJECT, session_id="uuid-backend", source="startup"
        )

        orch.inject_slash_command_when_ready.assert_not_called()
