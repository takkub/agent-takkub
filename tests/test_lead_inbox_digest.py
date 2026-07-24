"""Targeted tests for the debounced Lead Inbox Digest."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent_takkub.lead_inbox import _INBOX_DIGEST_WINDOW_MS
from agent_takkub.orchestrator import Orchestrator

PROJECT = "digest-test"


def _lead_pane() -> MagicMock:
    pane = MagicMock()
    pane.session = MagicMock()
    pane.session.is_alive = True
    pane.session.is_at_ready_prompt.return_value = True
    return pane


def _written(session: MagicMock) -> str:
    parts: list[str] = []
    for call in session.write.call_args_list:
        value = call.args[0]
        if isinstance(value, bytes):
            parts.append(value.decode("utf-8", "replace"))
        else:
            parts.append(str(value))
    return "".join(parts)


@pytest.fixture
def orch(monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or PROJECT),
    )
    instance = Orchestrator()
    instance._idle_watchdog.stop()
    instance._panes_by_project[PROJECT] = {"lead": _lead_pane()}
    return instance


def test_default_window_debounces_burst_and_renders_single_digest(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TAKKUB_INBOX_DIGEST_MS", raising=False)
    timers: list[tuple[int, object]] = []

    with patch(
        "agent_takkub.lead_inbox.QTimer.singleShot",
        side_effect=lambda ms, callback: timers.append((ms, callback)),
    ):
        orch._notify_lead(PROJECT, "[backend#1 done] Refactored authentication middleware")
        orch._notify_lead(PROJECT, "[CC] [frontend → backend] Updated API contracts")

        lead = orch._panes_by_project[PROJECT]["lead"]
        lead.session.write.assert_not_called()
        assert [ms for ms, _ in timers] == [
            _INBOX_DIGEST_WINDOW_MS,
            _INBOX_DIGEST_WINDOW_MS,
        ]

        # The first callback is stale because the second notice restarted the
        # debounce window.
        timers[0][1]()
        lead.session.write.assert_not_called()

        timers[1][1]()

    written = _written(lead.session)
    assert "📬 [Lead Inbox Digest — 2 updates]" in written
    assert "• [backend#1] done: Refactored authentication middleware" in written
    assert "• [CC from frontend -> backend]: Updated API contracts" in written
    assert written.count("Lead Inbox Digest") == 1


def test_single_notice_waits_at_most_one_configured_window(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TAKKUB_INBOX_DIGEST_MS", "125")
    timers: list[tuple[int, object]] = []

    with patch(
        "agent_takkub.lead_inbox.QTimer.singleShot",
        side_effect=lambda ms, callback: timers.append((ms, callback)),
    ):
        orch._notify_lead(PROJECT, "[qa done] Smoke tests passed")
        lead = orch._panes_by_project[PROJECT]["lead"]
        lead.session.write.assert_not_called()
        assert timers[0][0] == 125

        timers[0][1]()

    assert "📬 [Lead Inbox Digest — 1 update]" in _written(lead.session)


def test_zero_window_preserves_immediate_legacy_delivery(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TAKKUB_INBOX_DIGEST_MS", "0")
    lead = orch._panes_by_project[PROJECT]["lead"]

    with patch("agent_takkub.lead_inbox.QTimer.singleShot"):
        orch._notify_lead(PROJECT, "[backend done] legacy")

    written = _written(lead.session)
    assert "[backend done] legacy" in written
    assert "Lead Inbox Digest" not in written


@pytest.mark.parametrize(
    "notice",
    [
        "[qa FAILED] checkout is broken",
        "⚠️ [spawn-failed] backend pane failed",
        "⚠️ [delivery-unconfirmed] frontend did not reach ready",
    ],
)
def test_blocking_notices_bypass_digest(
    orch: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
    notice: str,
) -> None:
    monkeypatch.setenv("TAKKUB_INBOX_DIGEST_MS", "60000")
    lead = orch._panes_by_project[PROJECT]["lead"]

    with patch("agent_takkub.lead_inbox.QTimer.singleShot"):
        orch._notify_lead(PROJECT, notice)

    assert notice in _written(lead.session)
    assert not orch._lead_digest_queue.get(PROJECT)


def test_blocking_notice_does_not_wait_behind_pending_digest(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TAKKUB_INBOX_DIGEST_MS", "60000")
    lead = orch._panes_by_project[PROJECT]["lead"]

    with patch("agent_takkub.lead_inbox.QTimer.singleShot"):
        orch._notify_lead(PROJECT, "[backend done] informational")
        orch._notify_lead(PROJECT, "[qa FAILED] production checkout is broken")

    written = _written(lead.session)
    assert "[qa FAILED] production checkout is broken" in written
    assert "Lead Inbox Digest" not in written
    assert list(orch._lead_digest_queue[PROJECT]) == ["[backend done] informational"]


def test_auto_chain_flushes_done_digest_first_and_is_not_window_delayed(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TAKKUB_INBOX_DIGEST_MS", "60000")
    timers: list[tuple[int, object]] = []
    lead = orch._panes_by_project[PROJECT]["lead"]

    with patch(
        "agent_takkub.lead_inbox.QTimer.singleShot",
        side_effect=lambda ms, callback: timers.append((ms, callback)),
    ):
        orch._notify_lead(PROJECT, "[frontend done] UI complete")
        assert not lead.session.write.called

        # This call must invalidate the digest wait. It writes the digest now
        # with the handoff directly behind it in the same Lead turn.
        orch._notify_lead(PROJECT, "[auto-chain handoff] run final verification")
        orch._pump_lead_notify(PROJECT)

        # The now-stale 60s callback cannot redeliver anything.
        timers[0][1]()

    written = _written(lead.session)
    digest_pos = written.index("Lead Inbox Digest")
    handoff_pos = written.index("[auto-chain handoff]")
    assert digest_pos < handoff_pos
    assert written.count("Lead Inbox Digest") == 1
    assert written.count("[auto-chain handoff]") == 1
    assert not orch._lead_notify_queue.get(PROJECT)


def test_old_timer_cannot_flush_a_new_burst_after_early_handoff(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TAKKUB_INBOX_DIGEST_MS", "60000")
    timers: list[tuple[int, object]] = []

    with patch(
        "agent_takkub.lead_inbox.QTimer.singleShot",
        side_effect=lambda ms, callback: timers.append((ms, callback)),
    ):
        orch._notify_lead(PROJECT, "[frontend done] first burst")
        first_timer = timers[0][1]
        orch._notify_lead(PROJECT, "[auto-chain handoff] verify first burst")

        orch._notify_lead(PROJECT, "[backend done] second burst")
        lead = orch._panes_by_project[PROJECT]["lead"]
        before = _written(lead.session)

        first_timer()

    assert _written(lead.session) == before
    assert list(orch._lead_digest_queue[PROJECT]) == ["[backend done] second burst"]
