"""Tests for the assign hard-timeout delivery warning (issue #26).

When _send_when_ready polls is_at_ready_prompt() but the pane never signals
ready (cold re-spawn render differs), it pastes blind at the 45s hard timeout.
That paste can be swallowed, leaving the pane empty while the Lead believes the
task landed. The fix surfaces the unconfirmed delivery to the Lead so
delegation stops failing silently.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _live_session() -> MagicMock:
    s = MagicMock()
    s.is_alive = True
    s.write = MagicMock()
    return s


def _pane(session=None) -> MagicMock:
    p = MagicMock()
    p.session = session
    return p


@pytest.fixture
def orch(qapp, monkeypatch) -> Orchestrator:
    o = Orchestrator.__new__(Orchestrator)
    QObject.__init__(o)
    o._panes_by_project = {}
    monkeypatch.setattr(o, "_resolve_project", lambda p=None: p or "P")
    monkeypatch.setattr(
        o, "_project_panes", lambda p=None: o._panes_by_project.get(o._resolve_project(p), {})
    )
    return o


class TestDeliveryUnconfirmedWarning:
    def test_warns_lead_with_role_and_issue_ref(self, orch: Orchestrator) -> None:
        lead = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead, "reviewer": _pane(_live_session())}
        with (
            patch("agent_takkub.orchestrator._log_event"),
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
        ):
            orch._warn_lead_delivery_unconfirmed("reviewer", "P")
        assert lead.session.write.called
        msg = lead.session.write.call_args[0][0]
        assert "reviewer" in msg
        assert "#26" in msg

    def test_warning_reports_actual_wait_window(self, orch: Orchestrator) -> None:
        lead = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead, "qa": _pane(_live_session())}
        with (
            patch("agent_takkub.orchestrator._log_event"),
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
        ):
            orch._warn_lead_delivery_unconfirmed("qa", "P", 90_000)
        msg = lead.session.write.call_args[0][0]
        assert "90s" in msg
        assert "45s" not in msg

    def test_noop_when_target_is_lead(self, orch: Orchestrator) -> None:
        lead = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead}
        with patch("agent_takkub.orchestrator._log_event"):
            orch._warn_lead_delivery_unconfirmed("lead", "P")
        lead.session.write.assert_not_called()

    def test_noop_when_no_live_lead(self, orch: Orchestrator) -> None:
        # Lead absent — must not raise.
        orch._panes_by_project["P"] = {"reviewer": _pane(_live_session())}
        with patch("agent_takkub.orchestrator._log_event"):
            orch._warn_lead_delivery_unconfirmed("reviewer", "P")

    def test_hard_timeout_pastes_and_warns(self, orch: Orchestrator, monkeypatch) -> None:
        """End-to-end: a never-ready pane hits the timeout → task pasted
        best-effort AND the Lead is warned (delegation no longer silent)."""
        lead = _pane(_live_session())
        reviewer = _pane(_live_session())
        reviewer.session.is_at_ready_prompt.return_value = False  # never ready
        orch._panes_by_project["P"] = {"lead": lead, "reviewer": reviewer}
        # Run scheduled callbacks synchronously so the poll loop completes.
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.orchestrator._log_event"):
            orch._send_when_ready("reviewer", "run smoke", max_wait_ms=1000, project="P")

        # Best-effort paste still happened on the reviewer pane...
        assert reviewer.session.write.called
        # ...and the Lead got the unconfirmed-delivery warning.
        warnings = [c.args[0] for c in lead.session.write.call_args_list if c.args]
        assert any("#26" in msg for msg in warnings)
        assert any("1s" in msg for msg in warnings)


class TestGateDeferredSpawnDeliversEventually:
    """2026-07-11 dogfooding bug: an assign() whose spawn was deferred by the
    ConPTY safety gate (modal/popup blocking construction) for longer than
    max_wait_ms silently dropped the task — _check() gave up the moment
    elapsed[0] crossed max_wait_ms even though pane.session was still None,
    with no paste and no Lead warning. The gate's own retry loop
    (spawn_engine._retry_deferred_spawn) has no timeout and keeps trying
    every 50ms until it clears, so the poller must keep waiting too as long
    as the role stays in the gate's deferred set."""

    def test_polls_past_timeout_while_gate_deferred_then_delivers(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        reviewer = _pane(session=None)  # no session yet — spawn still deferred
        orch._panes_by_project["P"] = {"lead": _pane(_live_session()), "reviewer": reviewer}
        # Role is parked in the gate's deferred set the whole time the poll
        # would otherwise time out.
        orch._spawn_deferred = {"P::reviewer"}

        calls = {"n": 0}

        def _fake_single_shot(_ms, fn):
            calls["n"] += 1
            if calls["n"] == 5:
                # Gate clears and the pane finally comes alive, mid-poll —
                # exactly like the real spawn succeeding after several
                # deferred retries.
                orch._spawn_deferred.discard("P::reviewer")
                session = _live_session()
                session.is_at_ready_prompt.return_value = True
                reviewer.session = session
            fn()

        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(_fake_single_shot))

        with patch("agent_takkub.orchestrator._log_event"):
            # max_wait_ms is tiny — far shorter than the 5 polls it takes for
            # the gate to clear — so a correct fix must keep polling anyway.
            orch._send_when_ready("reviewer", "run smoke", max_wait_ms=300, project="P")

        assert reviewer.session.write.called
        # No unconfirmed-delivery warning: this was a clean, confirmed
        # ready-prompt delivery, not a blind/timeout paste.
        lead = orch._panes_by_project["P"]["lead"]
        assert not any("#26" in c.args[0] for c in lead.session.write.call_args_list if c.args)

    def test_gives_up_and_warns_once_truly_not_gate_deferred(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """A pane that never gets a session AND is never in the gate's
        deferred set (e.g. spawn silently failed some other way) must still
        hit the hard timeout and warn the Lead — no infinite hang, no silent
        drop either."""
        reviewer = _pane(session=None)
        lead = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead, "reviewer": reviewer}
        orch._spawn_deferred = set()
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.orchestrator._log_event"):
            orch._send_when_ready("reviewer", "run smoke", max_wait_ms=300, project="P")

        assert any("#26" in c.args[0] for c in lead.session.write.call_args_list if c.args)


class TestReadyWaitMs:
    """agy (gemini role's engine) cold-boots ~46s — right at the 45s default
    edge — forcing a fragile blind paste (#26). gemini/agy panes get a longer
    ready window; an explicit caller override always wins."""

    def test_gemini_gets_longer_window_on_default(self, orch, monkeypatch) -> None:
        from agent_takkub import provider_config

        monkeypatch.setattr(
            provider_config, "effective_provider_for", lambda role, project=None: "gemini"
        )
        assert orch._ready_wait_ms("gemini", "P", 45_000) == 90_000

    def test_codex_gets_longer_window_on_default(self, orch, monkeypatch) -> None:
        from agent_takkub import provider_config

        monkeypatch.setattr(
            provider_config, "effective_provider_for", lambda role, project=None: "codex"
        )
        assert orch._ready_wait_ms("codex", "P", 45_000) == 90_000

    def test_claude_role_without_mcps_keeps_default(self, orch, monkeypatch) -> None:
        from agent_takkub import pane_tools_policy, provider_config

        monkeypatch.setattr(
            provider_config, "effective_provider_for", lambda role, project=None: "claude"
        )
        monkeypatch.setattr(
            pane_tools_policy,
            "effective_mcps",
            lambda role, default=None: frozenset(),
        )
        assert orch._ready_wait_ms("backend", "P", 45_000) == 45_000

    def test_claude_role_with_policy_mcps_gets_longer_window(self, orch, monkeypatch) -> None:
        from agent_takkub import pane_tools_policy, provider_config, shared_dev_tools

        seen = {}

        monkeypatch.setattr(
            provider_config, "effective_provider_for", lambda role, project=None: "claude"
        )
        monkeypatch.setattr(
            shared_dev_tools,
            "default_role_mcp_policy",
            lambda: {"qa": frozenset({"playwright", "chrome-devtools"})},
        )

        def _effective_mcps(role, default=None):
            seen["role"] = role
            seen["default"] = default
            return default

        monkeypatch.setattr(pane_tools_policy, "effective_mcps", _effective_mcps)

        assert orch._ready_wait_ms("qa", "P", 45_000) == 90_000
        assert seen == {
            "role": "qa",
            "default": frozenset({"playwright", "chrome-devtools"}),
        }

    def test_claude_shard_uses_base_role_mcp_policy(self, orch, monkeypatch) -> None:
        from agent_takkub import pane_tools_policy, provider_config, shared_dev_tools

        monkeypatch.setattr(
            provider_config, "effective_provider_for", lambda role, project=None: "claude"
        )
        monkeypatch.setattr(
            shared_dev_tools,
            "default_role_mcp_policy",
            lambda: {"critic": frozenset({"playwright"})},
        )
        monkeypatch.setattr(
            pane_tools_policy,
            "effective_mcps",
            lambda role, default=None: default if role == "critic" else frozenset(),
        )

        assert orch._ready_wait_ms("critic#2", "P", 45_000) == 90_000

    def test_explicit_override_wins_even_for_gemini(self, orch, monkeypatch) -> None:
        from agent_takkub import provider_config

        # The peer-send short-poll path passes a small explicit wait — it must
        # NOT be bumped to 90s just because the role resolves to agy.
        monkeypatch.setattr(
            provider_config, "effective_provider_for", lambda role, project=None: "gemini"
        )
        assert orch._ready_wait_ms("gemini", "P", 1000) == 1000

    def test_provider_lookup_failure_falls_back_to_default(self, orch, monkeypatch) -> None:
        from agent_takkub import provider_config

        def boom(role, project=None):
            raise RuntimeError("provider probe failed")

        monkeypatch.setattr(provider_config, "effective_provider_for", boom)
        assert orch._ready_wait_ms("gemini", "P", 45_000) == 45_000


class TestVerifiedEnterWiring:
    """The swallowed-Enter self-heal (#22) must cover teammate-bound deliveries,
    not just Lead notices — _send_when_ready's task paste and the peer `send`
    paste are the documented victims (a pane stuck on `[Pasted text]` forever).
    Both must route the submit through _delayed_enter_verified, never plain
    _delayed_enter."""

    def test_task_deliver_uses_verified_enter(self, orch: Orchestrator, monkeypatch) -> None:
        reviewer = _pane(_live_session())
        reviewer.session.is_at_ready_prompt.return_value = True  # ready → deliver now
        orch._panes_by_project["P"] = {"lead": _pane(_live_session()), "reviewer": reviewer}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))
        with (
            patch("agent_takkub.orchestrator._log_event"),
            patch("agent_takkub.orchestrator._delayed_enter_verified") as verified,
            patch("agent_takkub.orchestrator._delayed_enter") as plain,
        ):
            orch._send_when_ready("reviewer", "run smoke", max_wait_ms=1000, project="P")
        verified.assert_called_once()
        assert verified.call_args[0][1] is reviewer.session
        plain.assert_not_called()

    def test_agy_task_deliver_verifies_expected_content(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """#126: agy uses the generic verified-submit path with its task fragment."""
        from agent_takkub import provider_config

        task = "[ROLE: qa] read the task spec and run the full suite"
        qa = _pane(_live_session())
        qa.session.is_at_ready_prompt.return_value = True
        orch._panes_by_project["P"] = {"lead": _pane(_live_session()), "qa": qa}
        monkeypatch.setattr(
            provider_config,
            "effective_provider_for",
            lambda role, project=None: "gemini",
        )
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with (
            patch("agent_takkub.orchestrator._log_event"),
            patch("agent_takkub.orchestrator._delayed_enter_verified") as verified,
            patch("agent_takkub.orchestrator._delayed_enter") as plain,
        ):
            orch._send_when_ready("qa", task, max_wait_ms=1000, project="P")

        verified.assert_called_once()
        assert verified.call_args[0][1] is qa.session
        assert verified.call_args.kwargs["content_fragment"] == task
        assert verified.call_args.kwargs["payload"]
        plain.assert_not_called()

    def test_peer_send_uses_verified_enter(self, orch: Orchestrator, monkeypatch) -> None:
        reviewer = _pane(_live_session())
        orch._panes_by_project["P"] = {"reviewer": reviewer}
        monkeypatch.setattr(orch, "_ps", lambda key: MagicMock())
        with (
            patch("agent_takkub.orchestrator._log_event"),
            patch("agent_takkub.orchestrator._delayed_enter_verified") as verified,
            patch("agent_takkub.orchestrator._delayed_enter") as plain,
        ):
            ok, _ = orch.send("reviewer", "hello peer", project="P")
        assert ok
        verified.assert_called_once()
        assert verified.call_args[0][1] is reviewer.session
        plain.assert_not_called()


class TestSpawnFailureNotSilent:
    """#26 root cause: when spawn can't register the pane (main_window routing
    desync), assign must NOT silently drop — it logs and warns the Lead."""

    def test_spawn_logs_and_returns_false_when_pane_absent(self, orch: Orchestrator) -> None:
        # No main_window is connected to paneRequested, so the pane never gets
        # created/registered — spawn must surface that, not return a bare False.
        orch._idle_state = {}
        orch._pane_state = {}
        with patch("agent_takkub.orchestrator._log_event") as log:
            ok, msg = orch.spawn("reviewer", cwd=None, project="P")
        assert ok is False
        assert "could not create pane" in msg
        assert any(c.args and c.args[0] == "spawn_failed" for c in log.call_args_list)

    def test_assign_warns_lead_when_spawn_fails(self, orch: Orchestrator) -> None:
        lead = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead}
        orch._idle_state = {}
        orch._pane_state = {}
        with (
            patch("agent_takkub.orchestrator._log_event"),
            patch("agent_takkub.orchestrator.QTimer.singleShot"),
        ):
            ok, _msg = orch.assign("reviewer", cwd=None, task="do it", project="P")
        assert ok is False
        assert any("spawn-failed" in c.args[0] for c in lead.session.write.call_args_list if c.args)

    def test_warn_spawn_failed_noop_for_lead_role(self, orch: Orchestrator) -> None:
        lead = _pane(_live_session())
        orch._panes_by_project["P"] = {"lead": lead}
        with patch("agent_takkub.orchestrator._log_event"):
            orch._warn_lead_spawn_failed("lead", "P", "x")
        lead.session.write.assert_not_called()

    def test_warn_spawn_failed_noop_without_live_lead(self, orch: Orchestrator) -> None:
        orch._panes_by_project["P"] = {"reviewer": _pane(_live_session())}
        with patch("agent_takkub.orchestrator._log_event"):
            orch._warn_lead_spawn_failed("reviewer", "P", "x")  # must not raise


class TestSlowBootSubmitBudget:
    """A codex/agy pane still cold-booting its MCP servers reads not-ready with
    the pasted task visibly pending in the composer. The submit CR must keep
    being resent on a SEPARATE, generous budget — not the tiny 3-resend swallow
    budget — otherwise a slow boot under concurrent multi-spawn exhausts the
    budget and strands the task unsubmitted (2026-07-13 dogfooding: 4 codex
    backends spawned together, the later 3 never submitted; the task pointer +
    idle auto-reminders piled up in the composer)."""

    def test_default_busy_budget_far_exceeds_swallow_budget(self) -> None:
        from agent_takkub.lead_inbox import _SUBMIT_BUSY_MAX_RESENDS, _SUBMIT_MAX_RESENDS

        # ≈90 s at the 600 ms grace — must cover a codex/agy cold boot, and be
        # far larger than the swallow budget it used to (wrongly) share.
        assert _SUBMIT_BUSY_MAX_RESENDS >= 100
        assert _SUBMIT_BUSY_MAX_RESENDS > _SUBMIT_MAX_RESENDS * 20

    def test_booting_pane_keeps_nudging_past_swallow_budget(self, qapp, monkeypatch) -> None:
        import agent_takkub.lead_inbox as li

        # Run the verify/resend timers synchronously so the whole chain drains.
        monkeypatch.setattr(li.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        session = MagicMock()
        session.is_at_ready_prompt.return_value = False  # perpetually booting
        session.shows_pending_input.return_value = True  # paste still in composer
        session.seconds_since_output.return_value = 0.0
        session.last_output_monotonic.return_value = 0.0
        pane = _pane(session)

        # Small explicit busy budget keeps the synchronous recursion shallow
        # while still proving the boot path uses the LARGE budget, not the tiny
        # swallow one (pre-fix this branch decremented `remaining` → ~4 CRs).
        li._delayed_enter_verified(
            pane,
            session,
            0,
            busy_max_resends=25,
            payload="task spec",
            content_fragment="task spec",
        )

        crs = [c for c in session.write.call_args_list if c.args and c.args[0] == b"\r"]
        assert len(crs) > li._SUBMIT_MAX_RESENDS, (
            f"submit gave up after {len(crs)} CRs while the pane was still "
            "booting — the small swallow budget must not bound the boot case"
        )
        assert len(crs) >= 20  # used the generous busy budget

    def test_submit_stops_once_paste_leaves_composer(self, qapp, monkeypatch) -> None:
        """Once the CR lands (composer no longer shows the paste), the boot-nudge
        loop must stop — no unbounded CR spray into a pane that already
        submitted."""
        import agent_takkub.lead_inbox as li

        monkeypatch.setattr(li.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        state = {"cycles": 0}

        session = MagicMock()
        session.is_at_ready_prompt.return_value = False  # busy the whole time
        session.seconds_since_output.return_value = 0.0
        session.last_output_monotonic.return_value = 0.0

        def _pending(_fragment: str) -> bool:
            # Paste sits in the composer for a few boot cycles, then the CR lands
            # and it clears — the loop must not keep nudging after that.
            state["cycles"] += 1
            return state["cycles"] <= 5

        session.shows_pending_input.side_effect = _pending
        pane = _pane(session)

        li._delayed_enter_verified(
            pane,
            session,
            0,
            busy_max_resends=100,
            payload="task spec",
            content_fragment="task spec",
        )

        crs = [c for c in session.write.call_args_list if c.args and c.args[0] == b"\r"]
        # Nudged through the boot cycles, then stopped when the paste cleared —
        # bounded well under the 100 budget, not sprayed to exhaustion.
        assert 5 <= len(crs) <= 8
