"""Targeted regression test for #499: remote->lead (and any peer->peer)
`takkub send` delivery acks stayed stuck at "sent" forever even when the
target pane demonstrably took the message and replied, because
`_confirm_role_message` only trusted a "no longer at ready prompt" read at
settle time. A FAST reply (a short chat ack — the common case for `lead`,
the most continuously-active pane) can fully round-trip and return to its
ready prompt before the verify chain's own grace window elapses, so the
pane looks exactly like "never delivered" at the moment the check runs.

The fix mirrors the fallback task delivery already uses (#359): any PTY
output produced after the write's baseline timestamp proves the bytes were
received, regardless of the pane's current ready state.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import orchestrator as orch_mod
from agent_takkub import role_messages
from agent_takkub.orchestrator import Orchestrator


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp, tmp_path, monkeypatch) -> Orchestrator:
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
    monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)

    with (
        patch.object(Orchestrator, "_start_hot_md_timer", lambda self: None, create=True),
        patch("agent_takkub.orchestrator.Orchestrator._load_pending_cc", lambda self: None),
        patch(
            "agent_takkub.orchestrator.Orchestrator._start_browser_mcps",
            lambda self: None,
            create=True,
        ),
    ):
        o = Orchestrator.__new__(Orchestrator)
        from PyQt6.QtCore import QObject

        QObject.__init__(o)
        o._panes_by_project = {}
        o._pane_state = {}
        o._idle_state = {}
        o._recent_exits = {}
        o._recent_done = []
        o._pending_lead_cc = {}
    return o


def _record(orch: Orchestrator, project_ns: str = "p", to_role: str = "lead") -> str:
    return role_messages.append(
        orch_mod.RUNTIME_DIR,
        project_ns,
        to_role=to_role,
        from_role="remote",
        body="ลุยเลย",
        generation=1,
    )


def _state(orch: Orchestrator, project_ns: str, msg_id: str) -> str:
    rec = next(r for r in role_messages.read(orch_mod.RUNTIME_DIR, project_ns) if r["id"] == msg_id)
    return rec["state"]


class TestConfirmRoleMessageStillBusy:
    """Unchanged baseline behaviour: a pane still mid-turn at settle time is
    the ordinary, unambiguous "accepted" signal — no output evidence needed."""

    def test_marks_delivered_when_pane_left_ready_prompt(self, orch: Orchestrator) -> None:
        msg_id = _record(orch)
        session = MagicMock()
        session.is_at_ready_prompt.return_value = False

        orch._confirm_role_message("p", msg_id, session)

        assert _state(orch, "p", msg_id) == "delivered"


class TestConfirmRoleMessageNoEvidence:
    """A pane that is still ready AND never produced output since the write
    genuinely might never have received it — must stay unconfirmed, not be
    rubber-stamped just because a baseline was supplied."""

    def test_stays_unconfirmed_without_baseline(self, orch: Orchestrator) -> None:
        msg_id = _record(orch)
        session = MagicMock()
        session.is_at_ready_prompt.return_value = True

        orch._confirm_role_message("p", msg_id, session)

        assert _state(orch, "p", msg_id) == "sent"

    def test_stays_unconfirmed_when_no_output_since_baseline(self, orch: Orchestrator) -> None:
        msg_id = _record(orch)
        session = MagicMock()
        session.is_at_ready_prompt.return_value = True
        session.last_output_monotonic.return_value = 100.0

        orch._confirm_role_message("p", msg_id, session, write_baseline=100.0)

        assert _state(orch, "p", msg_id) == "sent"


class TestConfirmRoleMessageFastRoundTrip:
    """#499 — the actual reported bug: a target (typically `lead`) that
    fully processed and replied to the message before the verify chain's
    grace window elapsed must still be confirmed delivered, even though it
    reads as READY right now."""

    def test_marks_delivered_when_output_followed_the_write(self, orch: Orchestrator) -> None:
        msg_id = _record(orch)
        session = MagicMock()
        # Back at its ready prompt by the time settle fires (fast turn), but
        # last_output_monotonic() has clearly advanced past the write.
        session.is_at_ready_prompt.return_value = True
        session.last_output_monotonic.return_value = 105.0

        orch._confirm_role_message("p", msg_id, session, write_baseline=100.0)

        assert _state(orch, "p", msg_id) == "delivered"

    def test_ignores_non_numeric_output_timing_from_a_fake_session(
        self, orch: Orchestrator
    ) -> None:
        """A test double / degraded session without real timing must degrade
        to "no evidence" rather than raising (mirrors `_timing_or_none`'s own
        contract for task delivery)."""
        msg_id = _record(orch)
        session = MagicMock()
        session.is_at_ready_prompt.return_value = True
        session.last_output_monotonic.return_value = MagicMock()  # non-numeric

        orch._confirm_role_message("p", msg_id, session, write_baseline=100.0)

        assert _state(orch, "p", msg_id) == "sent"

    def test_missing_message_id_is_a_noop(self, orch: Orchestrator) -> None:
        session = MagicMock()
        session.is_at_ready_prompt.return_value = True
        orch._confirm_role_message("p", "", session, write_baseline=100.0)  # must not raise
