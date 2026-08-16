"""Targeted tests for #264: the Lead Inbox Digest's adaptive window.

Before this fix, `_INBOX_DIGEST_WINDOW_MS` (60s) applied in full even to a
solo `done()` with nothing else in the project still working — a prod user
described the cockpit as feeling like it "always hangs on something". The
fix: `_notify_lead` now shortens the armed window to
`_INBOX_DIGEST_ADAPTIVE_SETTLE_MS` whenever `_other_roles_still_active`
finds no other role in a non-terminal state, re-evaluated on every notice
added to a burst so a genuine multi-role burst still gets the full window
right up until its LAST item.

See test_lead_inbox_digest.py for the pre-existing debounce/combine
mechanics coverage (updated to register a still-working sibling role so it
keeps exercising the full-window path this file's tests deliberately don't
register).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent_takkub.lead_inbox import (
    _INBOX_DIGEST_ADAPTIVE_SETTLE_MS,
    _INBOX_DIGEST_WINDOW_MS,
)
from agent_takkub.orchestrator import Orchestrator

PROJECT = "adaptive-digest-test"


def _lead_pane() -> MagicMock:
    pane = MagicMock()
    pane.session = MagicMock()
    pane.session.is_alive = True
    pane.session.is_at_ready_prompt.return_value = True
    return pane


def _pane(state: str) -> MagicMock:
    pane = MagicMock()
    pane.state = state
    pane.session = MagicMock()
    return pane


@pytest.fixture
def orch(monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or PROJECT),
    )
    monkeypatch.delenv("TAKKUB_INBOX_DIGEST_MS", raising=False)
    instance = Orchestrator()
    instance._idle_watchdog.stop()
    instance._panes_by_project[PROJECT] = {"lead": _lead_pane()}
    return instance


def test_solo_completion_uses_the_short_settle_window(orch: Orchestrator) -> None:
    """The issue's headline complaint: a role that finishes alone, with no
    other role in the project still active, must not pay the full window."""
    timers: list[tuple[int, object]] = []

    with patch(
        "agent_takkub.lead_inbox.QTimer.singleShot",
        side_effect=lambda ms, callback: timers.append((ms, callback)),
    ):
        orch._notify_lead(PROJECT, "[qa done] smoke tests passed", from_role="qa")

    assert timers[0][0] == _INBOX_DIGEST_ADAPTIVE_SETTLE_MS
    assert timers[0][0] < _INBOX_DIGEST_WINDOW_MS


def test_burst_still_forming_keeps_the_full_window(orch: Orchestrator) -> None:
    """Another role still working when this notice lands — a real burst
    could still combine, so the intended "wake once per burst" behaviour
    must be preserved at the full window."""
    orch._panes_by_project[PROJECT]["devops"] = _pane("working")
    timers: list[tuple[int, object]] = []

    with patch(
        "agent_takkub.lead_inbox.QTimer.singleShot",
        side_effect=lambda ms, callback: timers.append((ms, callback)),
    ):
        orch._notify_lead(PROJECT, "[backend done] endpoint shipped", from_role="backend")

    assert timers[0][0] == _INBOX_DIGEST_WINDOW_MS


def test_last_item_in_a_burst_switches_to_the_short_window(orch: Orchestrator) -> None:
    """The adaptive check re-runs on EVERY notice added to the burst (each
    one re-arms the timer) — once the LAST role finishes, nothing else is
    left to wait for, so that final re-arm should use the short window even
    though earlier items in the same burst used the full one."""
    orch._panes_by_project[PROJECT]["devops"] = _pane("working")
    timers: list[tuple[int, object]] = []

    with patch(
        "agent_takkub.lead_inbox.QTimer.singleShot",
        side_effect=lambda ms, callback: timers.append((ms, callback)),
    ):
        # backend finishes first — devops is still working, full window.
        orch._notify_lead(PROJECT, "[backend done] endpoint shipped", from_role="backend")
        assert timers[-1][0] == _INBOX_DIGEST_WINDOW_MS

        # devops finishes next — nothing else left active, short window.
        orch._panes_by_project[PROJECT]["devops"].state = "done"
        orch._notify_lead(PROJECT, "[devops done] stack is up", from_role="devops")
        assert timers[-1][0] == _INBOX_DIGEST_ADAPTIVE_SETTLE_MS


def test_reporting_roles_own_pane_is_excluded_from_the_active_check(
    orch: Orchestrator,
) -> None:
    """`done()` calls `_notify_lead` BEFORE flipping its own pane to
    "done" — the reporting role's pane still reads "working" at this exact
    moment. Without excluding `from_role` by name, a solo completion would
    always see itself as "another active role" and never get the short
    window."""
    orch._panes_by_project[PROJECT]["backend"] = _pane("working")
    timers: list[tuple[int, object]] = []

    with patch(
        "agent_takkub.lead_inbox.QTimer.singleShot",
        side_effect=lambda ms, callback: timers.append((ms, callback)),
    ):
        orch._notify_lead(PROJECT, "[backend done] endpoint shipped", from_role="backend")

    assert timers[0][0] == _INBOX_DIGEST_ADAPTIVE_SETTLE_MS


def test_adaptive_window_never_exceeds_an_explicit_smaller_configured_window(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator-configured window smaller than the adaptive settle must
    still win — the adaptive path only ever shortens, never lengthens."""
    monkeypatch.setenv("TAKKUB_INBOX_DIGEST_MS", "500")
    timers: list[tuple[int, object]] = []

    with patch(
        "agent_takkub.lead_inbox.QTimer.singleShot",
        side_effect=lambda ms, callback: timers.append((ms, callback)),
    ):
        orch._notify_lead(PROJECT, "[qa done] smoke tests passed", from_role="qa")

    assert timers[0][0] == 500
