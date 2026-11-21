"""#1041: a working Lead whose CLI parks unsteered input until the turn ends
(codex Tab queue, agy) gets actionable reports inside the running turn, and
never collects progress updates as native follow-up turns."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub import lead_inbox
from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator
from agent_takkub.provider_spec import busy_input_waits_for_turn_end


@pytest.fixture
def busy_lead(monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setenv("TAKKUB_INBOX_DIGEST_MS", "15000")  # conftest turns it off
    orch = Orchestrator.__new__(Orchestrator)
    QObject.__init__(orch)
    orch._pane_state = {}
    orch._pending_done_notices = {}
    session = MagicMock()
    session.is_alive = True
    session.is_at_ready_prompt.return_value = False
    session.is_at_trust_prompt.return_value = False
    session.is_blocked_on_tty_prompt.return_value = None
    session.is_blocked_on_permission_prompt.return_value = None
    session.shows_pending_input.return_value = False
    session.display_lines.return_value = ["working"]
    pane = MagicMock()
    pane.model.provider_name = "codex"
    pane._session_generation = 1
    pane.session = session
    panes = {"lead": pane}
    monkeypatch.setattr(orch, "_resolve_project", lambda project=None: project or "p")
    monkeypatch.setattr(orch, "_project_panes", lambda project=None: panes)
    monkeypatch.setattr(orch, "_lead_can_accept_injection", lambda _project: True)
    monkeypatch.setattr(orch, "_save_pending_done_notices", MagicMock())
    monkeypatch.setattr(orch, "_mark_done_notices_delivered", MagicMock())
    monkeypatch.setattr(orch, "_other_roles_still_active", lambda *_a, **_k: True)
    monkeypatch.setattr(lead_inbox.QTimer, "singleShot", MagicMock())
    monkeypatch.setattr(lead_inbox.backlog, "review_items", lambda *_a, **_k: [])
    submits = []
    monkeypatch.setattr(
        orch_mod, "_delayed_enter_verified", lambda *args, **kwargs: submits.append(kwargs)
    )
    return orch, pane, submits


def _past_ceiling(orch):
    old = time.time() - lead_inbox.DIGEST_MAX_HOLD_S - 5
    orch._digest_hold_since = {"p": old}
    orch._lead_notify_auto_hold_since = {"p": old}
    orch._lead_notify_busy_since = {"p": old}


@pytest.mark.parametrize(
    "provider,expected",
    [("codex", True), ("gemini", True), ("claude", False), ("opencode", False), ("nope", False)],
)
def test_which_providers_park_unsteered_input(provider, expected):
    assert busy_input_waits_for_turn_end(provider) is expected


def test_progress_only_burst_stays_in_cockpit_past_the_hold_ceiling(busy_lead):
    orch, pane, submits = busy_lead
    orch._notify_lead("p", "[frontend progress] building", from_role="frontend", kind="progress")
    _past_ceiling(orch)

    assert orch._flush_lead_digest("p") is False
    assert len(orch._lead_digest_queue["p"]) == 1
    assert not submits and not pane.session.write.called


def test_done_replaces_held_progress_and_steers_the_running_turn(busy_lead):
    orch, _pane, submits = busy_lead
    orch._notify_lead("p", "[frontend progress] building", from_role="frontend", kind="progress")
    orch._notify_lead("p", "[frontend done] shipped the page", from_role="frontend", kind="done")
    _past_ceiling(orch)

    assert orch._flush_lead_digest("p") is True
    assert len(submits) == 1 and submits[0]["steer"] is True
    assert "shipped the page" in submits[0]["content_fragment"]
    assert "building" not in submits[0]["content_fragment"]


def test_failed_report_steers_and_passes_a_waiting_progress_item(busy_lead):
    orch, _pane, submits = busy_lead
    orch._enqueue_live_lead_notice("p", "[backend progress] half way")
    orch._enqueue_live_lead_notice("p", "[devops FAILED] compose would not start")
    _past_ceiling(orch)

    orch._pump_lead_notify("p")

    assert len(submits) == 1 and submits[0]["steer"] is True
    assert "compose would not start" in submits[0]["content_fragment"]
    assert orch._lead_notify_queue["p"][0][0].startswith("[backend progress]")
    submits[0]["on_settled"]()
    orch._pump_lead_notify("p")
    assert len(submits) == 1, "progress alone must keep waiting for the turn to end"


def test_progress_reaches_an_idle_lead_as_a_plain_submit(busy_lead):
    orch, pane, submits = busy_lead
    pane.session.is_at_ready_prompt.return_value = True
    orch._enqueue_live_lead_notice("p", "[backend progress] half way")

    orch._pump_lead_notify("p")

    assert len(submits) == 1 and submits[0]["steer"] is False


def test_steering_lead_keeps_the_existing_progress_ceiling(busy_lead):
    orch, pane, _submits = busy_lead
    pane.model.provider_name = "claude"
    orch._notify_lead("p", "[frontend progress] building", from_role="frontend", kind="progress")
    _past_ceiling(orch)

    assert orch._flush_lead_digest("p", arm_pump=False) is True
    assert "building" in orch._lead_notify_queue["p"][0][0]
