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

from unittest.mock import MagicMock, call

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
    empty_composer: bool = False,
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
    # (#343) resize() nudge target — real ints so `cols + 1` in the escalation
    # probe behaves like the real PtySession instead of silently no-oping on
    # a MagicMock's auto-generated __add__.
    s.cols = 100
    s.rows = 36
    s.resize = MagicMock()
    # Default false: most panes are NOT claude's empty-composer shape. Tests
    # covering the #343 auto-recovery path override this explicitly.
    s.is_at_claude_empty_composer = MagicMock(return_value=empty_composer)
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
    """Reaching the Nth consecutive occurrence fires the phase-1 nudge; the
    loud escalation itself only lands on the FOLLOWING tick (phase 2), once
    the probe has had a real chance to observe a post-redraw screen — see
    _nudge_stale_marker / _resolve_stale_marker_nudge."""
    _add_pane(orch, "projX", "lead", _sess(quiet=STALE_MARKER_QUIET_S + 10))
    events = _capture_events(monkeypatch)
    notify_calls = _capture_notify(monkeypatch)

    _tick_n_cooldowns(orch, _STALE_MARKER_ESCALATE_EVERY + 1)

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
    """Each escalation costs one extra tick over the raw streak multiple —
    the phase-2 resolve tick — so two escalations (at streak N and 2N) need
    2N + 2 ticks total, not 2N (see test_escalates_after_n_consecutive_occurrences)."""
    _add_pane(orch, "projX", "backend", _sess(quiet=STALE_MARKER_QUIET_S + 10))
    events = _capture_events(monkeypatch)
    _capture_notify(monkeypatch)

    _tick_n_cooldowns(orch, _STALE_MARKER_ESCALATE_EVERY * 2 + 2)

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

    _tick_n_cooldowns(orch, _STALE_MARKER_ESCALATE_EVERY + 1)

    escalations = [e for e in events if e[0] == "ready_marker_stale_prolonged"]
    assert len(escalations) == 1
    assert escalations[0][1]["last_progress_ts"] == 0.0
    assert escalations[0][1]["last_progress_age_s"] is None


# -- #343 follow-up: two-phase resize-nudge probe + claude structural fallback


def test_escalation_sends_exactly_one_resize_nudge(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The nudge is cols+1 then back to the ORIGINAL cols — two real size
    changes bracketing the original, never a keystroke, never a bare repeat
    of the current size (which an OS/ConPTY layer may not even forward) —
    fired exactly once by phase 1, with phase 2 (the following tick) reading
    the screen only, never resizing again."""
    sess = _sess(quiet=STALE_MARKER_QUIET_S + 10)
    _add_pane(orch, "projX", "lead", sess)
    _capture_events(monkeypatch)
    _capture_notify(monkeypatch)

    _tick_n_cooldowns(orch, _STALE_MARKER_ESCALATE_EVERY)
    assert sess.resize.call_args_list == [call(101, 36), call(100, 36)]

    # Phase 2 (the resolve tick) must not fire a second nudge.
    orch._check_stale_markers(1000.0 + _STALE_MARKER_ESCALATE_EVERY * (STALE_MARKER_COOLDOWN_S + 1))
    assert sess.resize.call_args_list == [call(101, 36), call(100, 36)]


def test_nudge_phase_1_logs_only_the_before_state(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 1 fires the redraw and logs what the screen looked like BEFORE
    it — it must NOT claim to know an "after" yet (see _nudge_stale_marker's
    docstring for why reading the screen synchronously right after resize()
    returns would just be the pre-redraw frame again, not real evidence)."""
    _add_pane(orch, "projX", "lead", _sess(quiet=STALE_MARKER_QUIET_S + 10))
    events = _capture_events(monkeypatch)
    _capture_notify(monkeypatch)

    _tick_n_cooldowns(orch, _STALE_MARKER_ESCALATE_EVERY)

    nudges = [e for e in events if e[0] == "ready_marker_nudge"]
    assert len(nudges) == 1
    payload = nudges[0][1]
    assert payload["streak"] == _STALE_MARKER_ESCALATE_EVERY
    assert payload["provider"] == "claude"
    assert payload["structural_before"] is False
    assert "reworded prompt" in payload["footer_before"]
    assert "footer_after" not in payload
    assert "recognised_after" not in payload
    # No loud escalation yet — that's phase 2's call, on the next tick.
    assert not [e for e in events if e[0] == "ready_marker_stale_prolonged"]


def test_nudge_phase_2_escalates_when_not_recovered(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the following tick's real post-redraw read still shows nothing a
    marker recognises, phase 2 logs the full before/after comparison and
    falls straight through to the loud escalation — in that same tick, not
    after another _STALE_MARKER_ESCALATE_EVERY streak."""
    _add_pane(orch, "projX", "lead", _sess(quiet=STALE_MARKER_QUIET_S + 10))
    events = _capture_events(monkeypatch)
    notify_calls = _capture_notify(monkeypatch)

    _tick_n_cooldowns(orch, _STALE_MARKER_ESCALATE_EVERY + 1)

    results = [e for e in events if e[0] == "ready_marker_nudge_result"]
    assert len(results) == 1
    payload = results[0][1]
    assert payload["streak"] == _STALE_MARKER_ESCALATE_EVERY
    assert payload["provider"] == "claude"
    assert payload["recognised_after"] is False
    assert payload["structural_recovered"] is False
    assert "reworded prompt" in payload["footer_before"]
    assert "reworded prompt" in payload["footer_after"]
    assert [e for e in events if e[0] == "ready_marker_stale_prolonged"]
    assert notify_calls
    assert not [e for e in events if e[0] == "ready_marker_nudge_recovered"]


def test_claude_empty_composer_stable_across_nudge_recovers_silently(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real #343 shape: claude, quiet, unrecognised by every text marker,
    but structurally the same empty bordered composer box on BOTH sides of a
    forced redraw — phase 1's before-read and phase 2's genuinely-later
    after-read (a separate tick, not a same-call re-read). No 🔴 escalation,
    no page to Lead — just a cleared streak, so the routine 🟡 spam stops on
    its own the next time a marker recognises the pane (nothing more to do —
    the pane was never actually stuck)."""
    key = "projX::lead"
    sess = _sess(quiet=STALE_MARKER_QUIET_S + 10, empty_composer=True)
    _add_pane(orch, "projX", "lead", sess)
    events = _capture_events(monkeypatch)
    notify_calls = _capture_notify(monkeypatch)

    _tick_n_cooldowns(orch, _STALE_MARKER_ESCALATE_EVERY + 1)

    recovered = [e for e in events if e[0] == "ready_marker_nudge_recovered"]
    assert len(recovered) == 1
    assert recovered[0][1]["reason"] == "structural"
    assert not [e for e in events if e[0] == "ready_marker_stale_prolonged"]
    assert not notify_calls
    assert key not in orch._stale_marker_streak
    assert key not in orch._stale_marker_nudged


def test_empty_composer_shape_ignored_for_non_claude_provider(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The structural fallback is claude-only (#343) — a non-claude provider
    showing the same bytes coincidentally must still escalate normally."""
    monkeypatch.setattr(
        "agent_takkub.provider_config.effective_provider_for",
        lambda *a, **k: "codex",
    )
    sess = _sess(quiet=STALE_MARKER_QUIET_S + 10, empty_composer=True)
    _add_pane(orch, "projX", "lead", sess)
    events = _capture_events(monkeypatch)
    notify_calls = _capture_notify(monkeypatch)

    _tick_n_cooldowns(orch, _STALE_MARKER_ESCALATE_EVERY + 1)

    results = [e for e in events if e[0] == "ready_marker_nudge_result"]
    assert results[0][1]["structural_recovered"] is False
    assert not [e for e in events if e[0] == "ready_marker_nudge_recovered"]
    assert [e for e in events if e[0] == "ready_marker_stale_prolonged"]
    assert notify_calls


def test_composer_stability_requires_both_sides_of_the_nudge(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shape caught only on ONE side of the redraw (e.g. mid-repaint when
    first observed) must NOT be treated as recovered — only a shape that
    survives the forced redraw unchanged counts (#343). Modelled with the
    screen genuinely changing BETWEEN phase 1 and phase 2 (two separate
    sweep ticks), not a canned before/after pair consumed within one call."""
    sess = _sess(quiet=STALE_MARKER_QUIET_S + 10, empty_composer=True)
    _add_pane(orch, "projX", "lead", sess)
    events = _capture_events(monkeypatch)
    notify_calls = _capture_notify(monkeypatch)

    # Phase 1: fires the nudge while the screen is still the empty composer.
    _tick_n_cooldowns(orch, _STALE_MARKER_ESCALATE_EVERY)
    assert sess.resize.call_args_list == [call(101, 36), call(100, 36)]

    # Between phase 1 and phase 2 the screen settles into something else —
    # the composer shape genuinely did not survive the redraw.
    sess.display_lines.return_value = ["", "» reworded prompt nobody knows", ""]
    sess.is_at_claude_empty_composer.return_value = False

    # Phase 2: the next sweep tick reads the (now different) post-redraw screen.
    orch._check_stale_markers(1000.0 + _STALE_MARKER_ESCALATE_EVERY * (STALE_MARKER_COOLDOWN_S + 1))

    results = [e for e in events if e[0] == "ready_marker_nudge_result"]
    assert len(results) == 1
    assert results[0][1]["structural_recovered"] is False
    assert [e for e in events if e[0] == "ready_marker_stale_prolonged"]
    assert notify_calls


# -- #343: the raw structural predicate itself --------------------------------


def test_is_claude_empty_composer_matches_the_real_captured_shape() -> None:
    """Byte-for-byte the shape captured live during the #343 episode
    (ready_marker_possibly_stale footer, 134 consecutive occurrences, same
    pane, identical every time)."""
    from agent_takkub.pty_session import _is_claude_empty_composer

    lines = ["─" * 160, "❯", "─" * 137, ""]
    assert _is_claude_empty_composer(lines) is True


def test_is_claude_empty_composer_rejects_leftover_input_text() -> None:
    from agent_takkub.pty_session import _is_claude_empty_composer

    lines = ["─" * 160, "❯ an unsent message still sitting here", "─" * 137]
    assert _is_claude_empty_composer(lines) is False


def test_is_claude_empty_composer_rejects_shell_prompt() -> None:
    from agent_takkub.pty_session import _is_claude_empty_composer

    lines = ["some earlier output line", "PS C:\\Users\\monch\\project>", ""]
    assert _is_claude_empty_composer(lines) is False


def test_is_claude_empty_composer_rejects_bare_prompt_without_borders() -> None:
    from agent_takkub.pty_session import _is_claude_empty_composer

    lines = ["some conversation text", "❯", "more conversation text"]
    assert _is_claude_empty_composer(lines) is False
