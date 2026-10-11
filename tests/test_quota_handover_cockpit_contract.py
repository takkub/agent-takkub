"""Quota replacements must keep their cockpit role, policy and task identity."""

from __future__ import annotations

import re
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub import lead_inbox, limit_autoresume
from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator
from agent_takkub.provider_spec import PROVIDER_REGISTRY


@pytest.fixture
def cockpit(monkeypatch):
    app = QCoreApplication.instance() or QCoreApplication([])
    orch = Orchestrator.__new__(Orchestrator)
    QObject.__init__(orch)
    orch._pane_state = {}
    orch._panes_by_project = {}
    orch._pipeline_runs = {}
    orch._notify_lead = MagicMock()
    orch._live_non_scaffolding_children = MagicMock(return_value=[])
    orch._warn_lead_delivery_uncertain = MagicMock()
    monkeypatch.setattr(orch, "_resolve_project", lambda project=None: project or "p")
    monkeypatch.setattr(orch, "_project_panes", lambda project=None: orch._panes_by_project["p"])
    monkeypatch.setattr(orch, "_ready_wait_ms", lambda role, project, wait: wait)
    session = MagicMock()
    session.is_alive = True
    session.is_at_ready_prompt.return_value = True
    session.is_at_trust_prompt.return_value = False
    session.is_blocked_on_tty_prompt.return_value = None
    session.is_blocked_on_permission_prompt.return_value = None
    session.shows_pending_input.return_value = False
    session.shows_busy_marker.return_value = False
    session.shows_account_pending_marker.return_value = False
    session.display_lines.return_value = ["unfinished user request"]
    pane = MagicMock()
    pane.model.provider_name = "codex"
    pane._session_generation = 1
    pane._session_cwd = "C:/project with spaces"
    pane._transcript_path = "previous-lead.log"
    pane.deferred_spawn = False
    pane.session = session
    orch._panes_by_project["p"] = {"lead": pane}
    yield orch, pane, app


@pytest.mark.parametrize(
    "provider,account",
    [(p, None) for p in PROVIDER_REGISTRY] + [("claude", "next"), ("codex", "next")],
)
def test_lead_provider_takeover_demands_cockpit_bootstrap(cockpit, monkeypatch, provider, account):
    orch, _pane, _app = cockpit
    previous = provider if account else "codex" if provider != "codex" else "claude"
    ps = orch._ps("p::lead")
    orch.close = MagicMock()
    orch.spawn = MagicMock(return_value=(True, "spawned"))
    orch._send_when_ready = MagicMock()
    monkeypatch.setattr(limit_autoresume.QTimer, "singleShot", lambda _delay, callback: callback())

    orch._reroute_pane_to_provider(
        "p", "lead", ps, provider, previous, time.time() + 3600, account=account
    )

    brief = orch._send_when_ready.call_args.args[1]
    assert "agent-takkub" in brief
    assert "takkub context read" in brief
    assert brief.index("takkub context read") < brief.index("unfinished user request")
    assert "takkub team status" in brief
    assert "takkub assign --mode pane" in brief
    assert "solo-lead" in brief and "auto" in brief and "full" in brief
    assert "takkub done" in brief and "Lead" in brief
    assert "previous-lead.log" in brief


@pytest.mark.parametrize(
    "provider,account", [("claude", "next"), ("codex", "next"), ("gemini", None)]
)
def test_reroute_keeps_the_registered_task_contract(cockpit, monkeypatch, provider, account):
    orch, pane, _app = cockpit
    orch._panes_by_project["p"] = {"backend": pane}
    ps = orch._ps("p::backend")
    ps.last_assigned_task = "Implement the pending fix"
    ps.task_id = "abc12345feed"
    ps.last_assigned_task_file = "pending-task.md"
    ps.last_assigned_scope = "deep"
    ps.backlog_id = "card-1"
    ps.assign_ts = 1234.0
    ps.budget_task_text = "Implement the pending fix"
    ps.task_token_total = 4321
    ps.task_spec_confirmation_digest = "approved-spec"
    expected = {
        name: getattr(ps, name)
        for name in (
            "task_id",
            "last_assigned_task_file",
            "last_assigned_scope",
            "backlog_id",
            "assign_ts",
            "budget_task_text",
            "task_token_total",
            "task_spec_confirmation_digest",
        )
    }
    orch.close = lambda role, **kwargs: orch._pane_state.pop(f"p::{role}")
    orch.spawn = MagicMock(return_value=(True, "spawned"))
    orch._send_when_ready = MagicMock()
    monkeypatch.setattr(limit_autoresume.QTimer, "singleShot", lambda _delay, callback: callback())

    orch._reroute_pane_to_provider(
        "p",
        "backend",
        ps,
        provider,
        provider if account else "codex",
        time.time() + 3600,
        account=account,
    )

    replacement = orch._ps("p::backend")
    assert {name: getattr(replacement, name) for name in expected} == expected
    assert replacement.account_override == account
    sent = orch._send_when_ready.call_args.args[1]
    assert "takkub done" in sent and "Implement the pending fix" in sent


def _deliver(cockpit, monkeypatch, role="lead", before_poll=None):
    orch, pane, _app = cockpit
    orch._panes_by_project["p"] = {role: pane}
    timers = []
    settled = []
    monkeypatch.setattr(
        lead_inbox.QTimer, "singleShot", lambda _delay, callback: timers.append(callback)
    )
    monkeypatch.setattr(
        "agent_takkub.orchestrator._delayed_enter_verified",
        lambda *args, **kwargs: settled.append(kwargs["on_settled"]),
    )
    orch._send_when_ready(role, "Continue the interrupted work", project="p")
    if before_poll:
        before_poll()
    for _ in range(30):
        if pane.session.write.called or not timers:
            break
        timers.pop(0)()
    return settled


@pytest.mark.parametrize(
    "role,field",
    [
        ("lead", "pending_lead_context"),
        ("backend", "pending_task_execution_context"),
        ("backend", "pending_memory_note"),
        ("backend", "pending_lang_directive"),
    ],
)
def test_direct_handover_delivers_fallback_and_clears_only_after_acceptance(
    cockpit,
    monkeypatch,
    role,
    field,
):
    orch, pane, _app = cockpit
    ps = orch._ps(f"p::{role}")
    policy = "Cockpit policy for a user-owned AGENTS.md: " + "keep the selected team setting. " * 30
    setattr(ps, field, policy)

    settled = _deliver(cockpit, monkeypatch, role)

    payload = pane.session.write.call_args.args[0]
    handoff_path = re.search(r'"([^"]+\.md)"', payload).group(1)
    delivered = Path(handoff_path).read_text(encoding="utf-8")
    assert policy in delivered
    assert "Continue the interrupted work" in delivered
    assert getattr(ps, field) == policy, "a paste alone does not prove acceptance"
    pane.session.is_at_ready_prompt.return_value = False
    pane.session.shows_busy_marker.return_value = True
    settled[0]()
    assert getattr(ps, field) is None


def test_policy_created_after_a_deferred_spawn_reaches_the_first_delivery(cockpit, monkeypatch):
    orch, pane, _app = cockpit
    ps = orch._ps("p::lead")
    policy = "Policy rendered when resource admission finally launches the replacement. " * 10

    _deliver(cockpit, monkeypatch, before_poll=lambda: setattr(ps, "pending_lead_context", policy))

    payload = pane.session.write.call_args.args[0]
    handoff_path = re.search(r'"([^"]+\.md)"', payload).group(1)
    assert policy in Path(handoff_path).read_text(encoding="utf-8")


@pytest.mark.parametrize("failure", ["handoff-file", "writer", "composer", "session-replaced"])
def test_failed_handover_retains_policy_for_retry(cockpit, monkeypatch, failure):
    orch, pane, _app = cockpit
    ps = orch._ps("p::lead")
    ps.pending_lead_context = "Required cockpit policy"
    if failure == "handoff-file":
        monkeypatch.setattr(lead_inbox, "_message_handoff_pointer", MagicMock(side_effect=OSError))
    elif failure == "writer":
        pane.session.write.return_value = False
    settled = _deliver(cockpit, monkeypatch)
    if failure == "composer":
        settled[0](SimpleNamespace(stuck_in_composer=True))
    elif failure == "session-replaced":
        pane.session = MagicMock()
        settled[0]()
    assert ps.pending_lead_context == "Required cockpit policy"


def test_quota_close_keeps_the_durable_assignment_open(cockpit, monkeypatch, tmp_path):
    from agent_takkub import task_ledger

    _old, pane, _app = cockpit
    orch = Orchestrator()
    orch.shutdown_timers()
    monkeypatch.setattr(orch, "_resolve_project", lambda project=None: project or "p")
    monkeypatch.setattr(orch, "_check_uncommitted_async", MagicMock())
    monkeypatch.setattr(orch, "_live_non_scaffolding_children", MagicMock(return_value=[]))
    monkeypatch.setattr(orch, "_send_when_ready", MagicMock())
    monkeypatch.setattr(orch, "spawn", MagicMock(return_value=(True, "spawned")))
    pane.state = "working"
    orch._panes_by_project["p"] = {"backend": pane}
    ps = orch._ps("p::backend")
    ps.last_assigned_task = "Read-only inspect existing work"
    ps.task_id = "abc12345feed"
    ps.task_delivered = True
    warning, _detail = task_ledger.create_assignment(
        "p",
        "backend",
        str(tmp_path),
        ps.last_assigned_task,
        "goal",
        "verify",
        "codex",
        task_id=ps.task_id,
    )
    assert not warning
    timers = []
    monkeypatch.setattr(limit_autoresume.QTimer, "singleShot", lambda _delay, fn: timers.append(fn))

    orch._reroute_pane_to_provider(
        "p", "backend", ps, "codex", "codex", time.time() + 3600, account="next"
    )

    active = task_ledger.open_impact("p", "backend")
    assert active is not None, "temporary recovery close must not abandon the continuing assignment"
    assert active["task_id"] == "abc12345feed"


def _lead_pump(cockpit, monkeypatch):
    orch, pane, _app = cockpit
    orch._notify_lead = Orchestrator._notify_lead.__get__(orch)
    orch._pending_done_notices = {}
    monkeypatch.setattr(orch, "_lead_can_accept_injection", lambda _project: True)
    monkeypatch.setattr(orch, "_save_pending_done_notices", MagicMock())
    monkeypatch.setattr(orch, "_mark_done_notices_delivered", MagicMock())
    monkeypatch.setattr(lead_inbox.QTimer, "singleShot", MagicMock())
    submits = []
    monkeypatch.setattr(
        orch_mod, "_delayed_enter_verified", lambda *args, **kwargs: submits.append(kwargs)
    )
    pane.session.is_at_ready_prompt.return_value = False
    return orch, pane, submits


def test_new_human_instructions_steer_busy_lead_immediately_in_order(cockpit, monkeypatch):
    orch, _pane, submits = _lead_pump(cockpit, monkeypatch)
    orch._enqueue_live_lead_notice("p", "📬 [Lead Inbox Digest — old]")

    orch._notify_lead("p", "First correction", from_role="user")
    orch._notify_lead("p", "Second correction", from_role="user")

    assert len(submits) == 1, "first correction must not wait for an idle Lead"
    assert submits[0]["steer"] is True and submits[0]["repaste"] is False
    assert submits[0]["payload"] is not None, "verification must still probe the composer"
    assert "First correction" in submits[0]["content_fragment"]
    submits[0]["on_settled"]()
    orch._pump_lead_notify("p")
    assert len(submits) == 2
    assert "Second correction" in submits[1]["content_fragment"]
    assert orch._lead_notify_queue["p"][0][0].startswith("📬")


@pytest.mark.parametrize("failure", ["dead-before-paste", "writer", "session-replaced", "no-lead"])
def test_human_priority_survives_lead_failure_and_durable_flush(cockpit, monkeypatch, failure):
    orch, pane, submits = _lead_pump(cockpit, monkeypatch)
    if failure == "dead-before-paste":
        orch._enqueue_live_lead_notice("p", "Keep the new requirement", human=True)
        pane.session.is_alive = False
        orch._pump_lead_notify("p")
    elif failure == "writer":
        pane.session.write.return_value = False
        orch._notify_lead("p", "Keep the new requirement", from_role="user")
    elif failure == "session-replaced":
        orch._notify_lead("p", "Keep the new requirement", from_role="user")
        old_session = pane.session
        pane.session = MagicMock()
        submits[0]["on_settled"]()
        pane.session = old_session
    else:
        orch._panes_by_project["p"] = {}
        assert not orch.inject_lead_prompt("Keep the new requirement", project="p")

    pending = orch._pending_done_notices["p"]
    assert len(pending) == 1 and pending[0]["role"] == "user"
    pane.session.is_alive = True
    pane.session.write.return_value = True
    orch._panes_by_project["p"] = {"lead": pane}
    submits.clear()
    orch._flush_pending_done_notices("p")
    assert len(submits) == 1 and submits[0]["steer"] is True
    assert "Keep the new requirement" in submits[0]["content_fragment"]


@pytest.mark.parametrize("failure", [None, "writer", "composer", "session-replaced"])
def test_notice_pump_clears_cockpit_policy_only_after_acceptance(cockpit, monkeypatch, failure):
    orch, pane, submits = _lead_pump(cockpit, monkeypatch)
    ps = orch._ps("p::lead")
    ps.pending_lead_context = "Required cockpit policy"
    if failure == "writer":
        pane.session.write.return_value = False

    orch._notify_lead("p", "New instruction", from_role="user")

    assert ps.pending_lead_context == "Required cockpit policy"
    if failure != "writer":
        if failure == "session-replaced":
            pane.session = MagicMock()
        submits[0]["on_settled"](SimpleNamespace(stuck_in_composer=failure == "composer"))
    assert ps.pending_lead_context == (None if failure is None else "Required cockpit policy")


def test_commands_during_account_handover_keep_task_identity_and_order(cockpit, monkeypatch):
    from agent_takkub import role_messages

    orch, pane, _app = cockpit
    orch._panes_by_project["p"] = {"backend": pane}
    orch._pending_lead_cc = {}
    ps = orch._ps("p::backend")
    ps.task_id = "continuing-task"
    ps.last_assigned_task = "Current work"

    def close(role, **kwargs):
        orch._panes_by_project["p"].pop(role)
        orch._pane_state.pop(f"p::{role}")

    orch.close = close
    orch.spawn = MagicMock(return_value=(True, "spawned"))
    orch._send_when_ready = MagicMock()
    timers = []
    monkeypatch.setattr(limit_autoresume.QTimer, "singleShot", lambda _delay, fn: timers.append(fn))
    orch._reroute_pane_to_provider(
        "p", "backend", ps, "codex", "codex", time.time() + 3600, account="next"
    )
    for correction in ("First correction", "Second correction"):
        ok, _detail = orch.send("backend", correction, from_role="lead", project="p")
        assert ok
    pending, _expired = role_messages.queued_no_pane_for_role(orch_mod.RUNTIME_DIR, "p", "backend")
    assert [rec["task_id"] for rec in pending] == ["continuing-task"] * 2

    # Finish the delayed respawn and deliver both corrections to the same task.
    timers.pop(0)()
    orch._panes_by_project["p"] = {"backend": pane}
    delivered = []
    orch.send = lambda role, text, **kwargs: delivered.append(text) or (True, "submitted")
    orch._flush_queued_no_pane_messages("p", "backend")
    assert delivered == ["First correction", "Second correction"]


@pytest.mark.parametrize("failure", [None, "writer", "composer", "session-replaced"])
def test_direct_human_send_steers_and_keeps_policy_until_confirmed(cockpit, monkeypatch, failure):
    orch, pane, submits = _lead_pump(cockpit, monkeypatch)
    ps = orch._ps("p::lead")
    ps.pending_lead_context = "Required cockpit policy"
    if failure == "writer":
        pane.session.write.return_value = False

    ok, detail = orch.send("lead", "New requirement", from_role="user", project="p")

    assert ok is (failure != "writer")
    assert ps.pending_lead_context == "Required cockpit policy"
    if failure == "writer":
        assert "receipt unconfirmed" in detail
        assert not submits
        return
    assert submits[0]["steer"] is True
    if failure == "session-replaced":
        pane.session = MagicMock()
    submits[0]["on_settled"](SimpleNamespace(stuck_in_composer=failure == "composer"))
    assert ps.pending_lead_context == (None if failure is None else "Required cockpit policy")


@pytest.mark.parametrize("provider,expected", [("gemini", "unsupported"), ("cursor", "unverified")])
def test_send_exposes_provider_steering_gaps_without_interrupting(
    cockpit, monkeypatch, provider, expected
):
    orch, pane, submits = _lead_pump(cockpit, monkeypatch)
    pane.model.provider_name = provider

    ok, detail = orch.send("lead", "New requirement", from_role="user", project="p")

    assert ok and f"active-turn steering {expected}" in detail
    assert submits[0]["steer"] is True
    assert all(call.args[0] != b"\x1b" for call in pane.session.write.call_args_list)


def test_rejected_flush_keeps_correction_durable(cockpit, monkeypatch):
    from agent_takkub import role_messages

    orch, pane, _app = cockpit
    orch._panes_by_project["p"] = {}
    assert orch.send("backend", "Keep the correction", from_role="lead", project="p")[0]
    orch._panes_by_project["p"] = {"backend": pane}
    pane.session.write.return_value = False

    orch._flush_queued_no_pane_messages("p", "backend")

    pending, _expired = role_messages.queued_no_pane_for_role(orch_mod.RUNTIME_DIR, "p", "backend")
    assert len(pending) == 1 and pending[0]["body"] == "Keep the correction"


def test_late_failure_callback_keeps_human_message_order(cockpit, monkeypatch):
    orch, pane, submits = _lead_pump(cockpit, monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(lead_inbox.time, "time", lambda: clock[0])
    orch._notify_lead("p", "First correction", from_role="user")
    pane.session.is_alive = False
    clock[0] += 1
    orch._notify_lead("p", "Second correction", from_role="user")
    submits[0]["on_settled"]()
    assert [item["body"] for item in orch._pending_done_notices["p"]] == [
        "First correction",
        "Second correction",
    ]


@pytest.mark.parametrize("sender", ["user", "remote"])
def test_direct_human_send_survives_lead_account_switch(cockpit, monkeypatch, sender):
    orch, pane, submits = _lead_pump(cockpit, monkeypatch)
    assert orch.send("lead", "New requirement during failover", from_role=sender, project="p")[0]
    pane.session.is_alive = False
    orch._panes_by_project["p"] = {}

    submits[0]["on_settled"]()

    pending = orch._pending_done_notices["p"]
    assert len(pending) == 1 and pending[0]["role"] == "user"
    assert pending[0]["body"] == "New requirement during failover"
