"""#20 structural layer: output-quiescence primitive + stale-marker detector.

The fragile part of detection is the natural-language markers — an upstream CLI
reword silently breaks is_at_ready_prompt and the idle watchdog stalls. We can't
replace the markers with exit codes (the CLI is a long-lived interactive TUI) but
we CAN add a structural signal — output quiescence — and use it to make the
silent break LOUD: a pane that is alive, has gone output-quiet (so it is not
mid-generation), and matches no state marker is almost certainly an idle prompt
we no longer recognise. These tests pin both pieces.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import (
    _STALE_MARKER_ESCALATE_EVERY,
    STALE_MARKER_COOLDOWN_S,
    STALE_MARKER_QUIET_S,
    Orchestrator,
)
from agent_takkub.pty_session import PtySession


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


# -- structural primitive: seconds_since_output ------------------------------


def test_seconds_since_output_inf_before_any_output() -> None:
    s = PtySession(cols=80, rows=24)
    assert s.seconds_since_output() == float("inf")


def test_seconds_since_output_small_right_after_feed() -> None:
    s = PtySession(cols=80, rows=24)
    s._feed_and_log(b"some streamed output\r\n")
    assert s.seconds_since_output() < 5.0


# -- stale-marker detector --------------------------------------------------


@pytest.fixture
def orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(Orchestrator, "_resolve_project", staticmethod(lambda p: p))
    o = Orchestrator()
    o.shutdown_timers()
    return o


def _sess(
    *,
    quiet: float,
    ready: bool = False,
    tty: object = None,
    trust: bool = False,
    splash: bool = False,
    lines: list[str] | None = None,
) -> MagicMock:
    s = MagicMock()
    s.is_alive = True
    s.seconds_since_output.return_value = quiet
    s.is_at_ready_prompt.return_value = ready
    s.is_blocked_on_tty_prompt.return_value = tty
    s.is_blocked_on_permission_prompt.return_value = None
    s.is_at_trust_prompt.return_value = trust
    s.is_at_update_splash.return_value = splash
    s.display_lines.return_value = lines or ["", "» reworded prompt nobody knows", ""]
    return s


def _capture_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(orch_mod, "_log_event", lambda ev, **k: events.append((ev, k)))
    return events


def _add_pane(orch: Orchestrator, project: str, role: str, sess: MagicMock) -> None:
    pane = MagicMock()
    pane.session = sess
    orch._panes_by_project[project] = {role: sess and pane}


def test_quiet_unrecognized_pane_is_flagged(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_pane(orch, "projX", "backend", _sess(quiet=STALE_MARKER_QUIET_S + 10))
    events = _capture_events(monkeypatch)
    orch._check_stale_markers(1000.0)
    stale = [e for e in events if e[0] == "ready_marker_possibly_stale"]
    assert len(stale) == 1
    assert stale[0][1]["project"] == "projX"
    assert "reworded prompt" in stale[0][1]["footer"]


def test_recognized_ready_pane_not_flagged(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_pane(orch, "projX", "backend", _sess(quiet=STALE_MARKER_QUIET_S + 10, ready=True))
    events = _capture_events(monkeypatch)
    orch._check_stale_markers(1000.0)
    assert not [e for e in events if e[0] == "ready_marker_possibly_stale"]


def test_streaming_pane_not_flagged(orch: Orchestrator, monkeypatch: pytest.MonkeyPatch) -> None:
    # Still producing output → genuinely busy, not blind.
    _add_pane(orch, "projX", "backend", _sess(quiet=2.0))
    events = _capture_events(monkeypatch)
    orch._check_stale_markers(1000.0)
    assert not [e for e in events if e[0] == "ready_marker_possibly_stale"]


def test_known_tty_prompt_not_flagged(orch: Orchestrator, monkeypatch: pytest.MonkeyPatch) -> None:
    _add_pane(
        orch, "projX", "backend", _sess(quiet=STALE_MARKER_QUIET_S + 10, tty="Ok to proceed? (y)")
    )
    events = _capture_events(monkeypatch)
    orch._check_stale_markers(1000.0)
    assert not [e for e in events if e[0] == "ready_marker_possibly_stale"]


def test_flag_is_rate_limited_per_pane(orch: Orchestrator, monkeypatch: pytest.MonkeyPatch) -> None:
    _add_pane(orch, "projX", "backend", _sess(quiet=STALE_MARKER_QUIET_S + 10))
    events = _capture_events(monkeypatch)
    orch._check_stale_markers(1000.0)
    orch._check_stale_markers(1001.0)  # within cooldown → no second log
    stale = [e for e in events if e[0] == "ready_marker_possibly_stale"]
    assert len(stale) == 1


# -- #343: escalation past N consecutive occurrences -------------------------


def _capture_notify(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    calls: list[tuple] = []
    monkeypatch.setattr(Orchestrator, "_notify_lead", lambda self, *a, **k: calls.append((a, k)))
    return calls


def _tick_n_cooldowns(orch: Orchestrator, n: int, start: float = 1000.0) -> None:
    """Fire _check_stale_markers n times, each one full cooldown apart —
    the same shape as the real #343 timeline (one routine log per ~600s)."""
    for i in range(n):
        orch._check_stale_markers(start + i * (STALE_MARKER_COOLDOWN_S + 1))


def test_no_escalation_before_the_threshold(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_pane(orch, "projX", "lead", _sess(quiet=STALE_MARKER_QUIET_S + 10))
    events = _capture_events(monkeypatch)
    _capture_notify(monkeypatch)

    _tick_n_cooldowns(orch, _STALE_MARKER_ESCALATE_EVERY - 1)

    assert not [e for e in events if e[0] == "ready_marker_stale_prolonged"]


def test_escalates_after_n_consecutive_occurrences(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_pane(orch, "projX", "lead", _sess(quiet=STALE_MARKER_QUIET_S + 10))
    events = _capture_events(monkeypatch)
    notify_calls = _capture_notify(monkeypatch)

    _tick_n_cooldowns(orch, _STALE_MARKER_ESCALATE_EVERY)

    escalations = [e for e in events if e[0] == "ready_marker_stale_prolonged"]
    assert len(escalations) == 1
    payload = escalations[0][1]
    assert payload["role"] == "lead"
    assert payload["project"] == "projX"
    assert payload["streak"] == _STALE_MARKER_ESCALATE_EVERY
    assert "reworded prompt" in payload["footer"]
    assert payload["markers_checked"] == [
        "is_at_ready_prompt",
        "is_blocked_on_tty_prompt",
        "is_at_trust_prompt",
        "is_blocked_on_permission_prompt",
        "is_at_update_splash",
    ]
    # escalating past the routine 🟡 line means actually telling someone —
    # even (especially) when the wedged pane IS Lead itself, per #343.
    assert len(notify_calls) == 1
    assert notify_calls[0][0][0] == "projX"
    assert "#343" in notify_calls[0][0][1]


def test_escalation_repeats_every_n_further_occurrences(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_pane(orch, "projX", "backend", _sess(quiet=STALE_MARKER_QUIET_S + 10))
    events = _capture_events(monkeypatch)
    _capture_notify(monkeypatch)

    _tick_n_cooldowns(orch, _STALE_MARKER_ESCALATE_EVERY * 2)

    escalations = [e for e in events if e[0] == "ready_marker_stale_prolonged"]
    assert len(escalations) == 2
    assert escalations[0][1]["streak"] == _STALE_MARKER_ESCALATE_EVERY
    assert escalations[1][1]["streak"] == _STALE_MARKER_ESCALATE_EVERY * 2


def test_recovery_resets_the_streak(orch: Orchestrator, monkeypatch: pytest.MonkeyPatch) -> None:
    key = "projX::backend"
    sess = _sess(quiet=STALE_MARKER_QUIET_S + 10)
    _add_pane(orch, "projX", "backend", sess)
    events = _capture_events(monkeypatch)
    _capture_notify(monkeypatch)

    _tick_n_cooldowns(orch, _STALE_MARKER_ESCALATE_EVERY - 1)
    assert orch._stale_marker_streak[key] == _STALE_MARKER_ESCALATE_EVERY - 1

    # Marker recognises the pane again — a genuine recovery, not a blip.
    sess.is_at_ready_prompt.return_value = True
    orch._check_stale_markers(9000.0)
    assert key not in orch._stale_marker_streak

    # Goes stale again: streak restarts from zero, so the same number of
    # further occurrences that used to trip escalation no longer does.
    sess.is_at_ready_prompt.return_value = False
    _tick_n_cooldowns(orch, _STALE_MARKER_ESCALATE_EVERY - 1, start=10000.0)
    assert not [e for e in events if e[0] == "ready_marker_stale_prolonged"]


def test_dead_pane_resets_the_streak(orch: Orchestrator, monkeypatch: pytest.MonkeyPatch) -> None:
    key = "projX::backend"
    sess = _sess(quiet=STALE_MARKER_QUIET_S + 10)
    _add_pane(orch, "projX", "backend", sess)
    _capture_events(monkeypatch)
    _capture_notify(monkeypatch)

    _tick_n_cooldowns(orch, _STALE_MARKER_ESCALATE_EVERY - 1)
    assert orch._stale_marker_streak[key] == _STALE_MARKER_ESCALATE_EVERY - 1

    sess.is_alive = False
    orch._check_stale_markers(9000.0)
    assert key not in orch._stale_marker_streak


def test_escalation_dump_never_raises_when_helpers_fail(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """provider/model/last-progress lookups are all best-effort — a broken
    one must not stop the event from being logged at all."""
    _add_pane(orch, "projX", "backend", _sess(quiet=STALE_MARKER_QUIET_S + 10))
    events = _capture_events(monkeypatch)
    _capture_notify(monkeypatch)
    monkeypatch.setattr(
        orch,
        "_compute_last_progress_ts",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    _tick_n_cooldowns(orch, _STALE_MARKER_ESCALATE_EVERY)

    escalations = [e for e in events if e[0] == "ready_marker_stale_prolonged"]
    assert len(escalations) == 1
    assert escalations[0][1]["last_progress_ts"] == 0.0
    assert escalations[0][1]["last_progress_age_s"] is None
