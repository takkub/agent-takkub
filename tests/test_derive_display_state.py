"""#263: `Orchestrator._derive_display_state` unifies pane.state (declared
at dispatch), the ready-marker screen scrape, and the delivery-health
notice signal into ONE state for `takkub list`/`status`'s display — see
that method's docstring for the full priority order. Real evidence from
the issue: gemini stuck on "Signing in..." read "working"; codex
genuinely busy (no task ever dispatched) read "active"; kimi stuck
needing `/login` read "active" too — all three hid ground truth the
screen already showed.

Level 1 (`TestPriorityOrder`): the priority chain in isolation via a
lightweight fake session whose 3 signal methods return canned
booleans/strings directly — proves ordering/gating regardless of what
real screen text produces those signals.

Level 2 (`TestRealTranscriptFixtures`): the SAME real
`PtySession.auth_failure_reason`/`shows_startup_marker`/
`is_hard_blocked_for` production methods, run unbound against real
captured screen text per provider (same `_FakeScreen`-delegate pattern
test_auth_failure_detection.py already uses) — proves the full pipeline
classifies a real transcript correctly, not just the priority logic in
the abstract.

Level 3 (`TestListStatusDetailedWiring`): `list_status_detailed` exposes
the result as an additive `"display_state"` key while leaving `"state"`
byte-identical to before — the #248/#247 pinned `_pane_display_state`
tests and `_resolve_role_wait_status` (lead_wait.py) both depend on
`"state"`'s literal value never changing.
"""

from __future__ import annotations

import collections
import types

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub.orchestrator import TOOL_STUCK_TIMEOUT_SEC, Orchestrator
from agent_takkub.provider_spec import AUTH_TRANSIENT_GRACE_SEC
from agent_takkub.pty_session import PtySession


def _pane(
    state: str, session: object | None = None, provider: str = "claude"
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        state=state,
        session=session,
        model=types.SimpleNamespace(provider_name=provider),
    )


class _StubSession:
    """Fake session whose 3 signal methods return exactly what's configured
    — isolates `_derive_display_state`'s priority/gating logic from what
    real screen text would produce those signals (see
    `TestRealTranscriptFixtures` for that side)."""

    def __init__(
        self,
        auth_reason: str | None = None,
        booting: bool = False,
        hard_blocked: bool = False,
        raise_on: tuple[str, ...] = (),
        tool_marker: str | None = None,
        seconds_since_output: float = 0.0,
        account_pending_reason: str | None = None,
    ) -> None:
        self._auth_reason = auth_reason
        self._booting = booting
        self._hard_blocked = hard_blocked
        self._raise_on = raise_on
        self._tool_marker = tool_marker
        self._seconds_since_output = seconds_since_output
        self._account_pending_reason = account_pending_reason
        self.is_alive = True

    def account_pending_reason(self, provider: str) -> str | None:
        if "account_pending" in self._raise_on:
            raise RuntimeError("boom")
        return self._account_pending_reason

    def auth_failure_reason(self, provider: str) -> str | None:
        if "auth" in self._raise_on:
            raise RuntimeError("boom")
        return self._auth_reason

    def tool_running_marker(self, provider: str) -> str | None:
        if "tool_stuck" in self._raise_on:
            raise RuntimeError("boom")
        return self._tool_marker

    def seconds_since_output(self) -> float:
        return self._seconds_since_output

    def shows_startup_marker(self) -> bool:
        if "booting" in self._raise_on:
            raise RuntimeError("boom")
        return self._booting

    def shows_boot_phase_marker(self) -> bool:
        # #281: the display state asks the boot-phase-only question now — the
        # wider `shows_startup_marker` also covers "mid-turn with a queued
        # message", which labelled a working codex pane "booting".
        if "booting" in self._raise_on:
            raise RuntimeError("boom")
        return self._booting

    def is_hard_blocked_for(self, provider: str) -> bool:
        if "busy" in self._raise_on:
            raise RuntimeError("boom")
        return self._hard_blocked


class TestPriorityOrder:
    def test_non_active_working_states_pass_through_unchanged(self) -> None:
        for state in ("spawning", "ready", "done", "empty", "pending-notice"):
            pane = _pane(state, session=_StubSession(auth_reason="send /login to login"))
            assert Orchestrator._derive_display_state(None, pane, state, False) == state

    def test_no_session_passes_through(self) -> None:
        pane = _pane("active", session=None)
        assert Orchestrator._derive_display_state(None, pane, "active", False) == "active"

    def test_login_required_beats_everything_else(self) -> None:
        session = _StubSession(auth_reason="send /login to login", booting=True, hard_blocked=True)
        pane = _pane("working", session=session)
        assert Orchestrator._derive_display_state(None, pane, "working", True) == "login-required"

    def test_account_pending_beats_login_required(self) -> None:
        # #346: an account/eligibility gate is NOT a login problem — it must
        # win ahead of login-required so Lead never sees the "log back in"
        # wording for a state that wouldn't be fixed by logging in.
        session = _StubSession(
            account_pending_reason="verifying your account",
            auth_reason="send /login to login",
            booting=True,
            hard_blocked=True,
        )
        pane = _pane("working", session=session)
        result = Orchestrator._derive_display_state(None, pane, "working", True)
        assert result == "blocked:provider-account"

    def test_no_account_pending_falls_through_to_login_required(self) -> None:
        session = _StubSession(account_pending_reason=None, auth_reason="send /login to login")
        pane = _pane("working", session=session)
        result = Orchestrator._derive_display_state(None, pane, "working", True)
        assert result == "login-required"

    def test_booting_beats_waiting_delivery(self) -> None:
        session = _StubSession(booting=True)
        pane = _pane("working", session=session)
        assert Orchestrator._derive_display_state(None, pane, "working", True) == "booting"

    def test_booting_applies_to_active_too(self) -> None:
        session = _StubSession(booting=True)
        pane = _pane("active", session=session)
        assert Orchestrator._derive_display_state(None, pane, "active", False) == "booting"

    def test_waiting_delivery_only_when_working_and_unconfirmed(self) -> None:
        session = _StubSession()
        pane = _pane("working", session=session)
        assert Orchestrator._derive_display_state(None, pane, "working", True) == "waiting-delivery"
        assert Orchestrator._derive_display_state(None, pane, "working", False) == "working"

    def test_busy_only_applies_to_active(self) -> None:
        session = _StubSession(hard_blocked=True)
        pane = _pane("active", session=session)
        assert Orchestrator._derive_display_state(None, pane, "active", False) == "busy"
        # "working" + hard-blocked is the NORMAL case (a real dispatched
        # task is genuinely generating) — not surfaced as anything special.
        pane_w = _pane("working", session=session)
        assert Orchestrator._derive_display_state(None, pane_w, "working", False) == "working"

    def test_unknown_only_for_active_uncalibrated_provider(self) -> None:
        session = _StubSession()
        pane = _pane("active", session=session, provider="cursor")
        assert Orchestrator._derive_display_state(None, pane, "active", False) == "unknown"

    def test_unknown_does_not_apply_to_calibrated_provider(self) -> None:
        session = _StubSession()
        pane = _pane("active", session=session, provider="claude")
        assert Orchestrator._derive_display_state(None, pane, "active", False) == "active"

    def test_unknown_does_not_apply_to_working(self) -> None:
        # A dispatched task is a fact the orchestrator itself knows,
        # independent of ready-marker calibration — must not be downgraded
        # to "unknown" just because the provider has no ready_rules.
        session = _StubSession()
        pane = _pane("working", session=session, provider="cursor")
        assert Orchestrator._derive_display_state(None, pane, "working", False) == "working"

    def test_each_signal_check_fails_open_on_exception(self) -> None:
        for which in ("account_pending", "auth", "booting", "busy", "tool_stuck"):
            session = _StubSession(raise_on=(which,))
            pane = _pane("active", session=session, provider="cursor")
            # No crash; falls through to whatever the next tier decides —
            # here that's "unknown" (cursor, active, no other signal fired).
            assert Orchestrator._derive_display_state(None, pane, "active", False) == "unknown"

    def test_tool_stuck_beats_waiting_delivery(self) -> None:
        # #308: ground-truth screen scrape wins over the delivery-health
        # notice signal, same as every other tier in this priority chain.
        session = _StubSession(
            tool_marker="running command", seconds_since_output=TOOL_STUCK_TIMEOUT_SEC
        )
        pane = _pane("working", session=session)
        assert Orchestrator._derive_display_state(None, pane, "working", True) == "stalled:tool"

    def test_tool_marker_before_timeout_is_not_stuck_yet(self) -> None:
        session = _StubSession(
            tool_marker="running command", seconds_since_output=TOOL_STUCK_TIMEOUT_SEC - 1
        )
        pane = _pane("working", session=session)
        result = Orchestrator._derive_display_state(None, pane, "working", True)
        assert result == "waiting-delivery"

    def test_tool_stuck_only_applies_to_working(self) -> None:
        # An "active" pane (never dispatched a task) has no shell-tool call
        # to be wedged in from the orchestrator's point of view.
        session = _StubSession(
            tool_marker="running command", seconds_since_output=TOOL_STUCK_TIMEOUT_SEC
        )
        pane = _pane("active", session=session)
        assert Orchestrator._derive_display_state(None, pane, "active", False) != "stalled:tool"

    def test_login_required_beats_tool_stuck(self) -> None:
        session = _StubSession(
            auth_reason="send /login to login",
            tool_marker="running command",
            seconds_since_output=TOOL_STUCK_TIMEOUT_SEC,
        )
        pane = _pane("working", session=session)
        assert Orchestrator._derive_display_state(None, pane, "working", False) == "login-required"

    def test_queued_resource_wait_beats_everything_else(self) -> None:
        """#412: a role literally parked in the resource-governor's queue for
        a NEW task cannot simultaneously be doing anything else — must win
        even over login-required, the previous top-priority tier."""
        session = _StubSession(auth_reason="send /login to login", booting=True, hard_blocked=True)
        pane = _pane("active", session=session)
        result = Orchestrator._derive_display_state(
            None, pane, "active", False, resource_wait_reason="heavy_project_limit"
        )
        assert result == "queued:heavy_project_limit"

    def test_queued_resource_wait_applies_even_to_terminal_pane_states(self) -> None:
        """Unlike every other tier, this one isn't gated behind base_state in
        ("active", "working") — the whole point is surfacing a queued NEW
        task regardless of the pane's stale previous state (done/empty/...)."""
        pane = _pane("done", session=None)
        result = Orchestrator._derive_display_state(
            None, pane, "done", False, resource_wait_reason="heavy_project_limit"
        )
        assert result == "queued:heavy_project_limit"

    def test_no_resource_wait_falls_through_unchanged(self) -> None:
        pane = _pane("active", session=None)
        result = Orchestrator._derive_display_state(
            None, pane, "active", False, resource_wait_reason=None
        )
        assert result == "active"


class _RealSignalSession:
    """Delegates to the REAL `PtySession.auth_failure_reason` /
    `shows_startup_marker` / `is_hard_blocked_for` production methods
    (unbound-call trick, same pattern test_auth_failure_detection.py's
    `_FakeScreen` uses) against captured provider screen text — proves the
    full pipeline classifies a real transcript, not just the priority
    logic in the abstract."""

    def __init__(self, lines: list[str], seconds_since_output: float = 100.0) -> None:
        self._lines = lines
        self._seconds_since_output = seconds_since_output
        self.is_alive = True

    def display_lines(self) -> list[str]:
        return self._lines

    def seconds_since_output(self) -> float:
        return self._seconds_since_output

    def auth_failure_reason(self, provider: str) -> str | None:
        return PtySession.auth_failure_reason(self, provider)

    def account_pending_reason(self, provider: str) -> str | None:
        return PtySession.account_pending_reason(self, provider)

    def shows_startup_marker(self) -> bool:
        return PtySession.shows_startup_marker(self)

    def shows_boot_phase_marker(self) -> bool:
        return PtySession.shows_boot_phase_marker(self)

    def is_hard_blocked_for(self, provider: str) -> bool:
        return PtySession.is_hard_blocked_for(self, provider)

    def tool_running_marker(self, provider: str) -> str | None:
        return PtySession.tool_running_marker(self, provider)


class TestRealTranscriptFixtures:
    """Real captured screen text per provider — mirrors the evidence in
    docs/audit/2026-08-16-263-264-266-notify-truth.md and reuses the same
    strings already confirmed in test_auth_failure_detection.py's
    TestGeminiColdBootNotSignedIn / TestKimiNotLoggedIn fixtures."""

    def test_gemini_stuck_verifying_account_past_grace_is_provider_account_blocked(
        self,
    ) -> None:
        # #346: verbatim live-captured screen from the issue (2026-08-22).
        # This must NOT read as "login-required" (nothing to log into) and
        # must NOT read as ready/active either — see the matching
        # is_at_ready_prompt regression test in test_pty_ready_prompt.py.
        session = _RealSignalSession(
            [
                "Antigravity CLI 1.1.17",
                "monchai500@gmail.com (Google AI Pro)",
                "Gemini 3.7 Flash (High)",
                "~/WebstormProjects/agent-takkub/worktrees/agent-takkub/gemini-1787380071",
                "",
                "⚠ Verifying your account...",
                "  We're finishing verifying your account eligibility.",
                "  This usually takes a moment. Please try again shortly.",
                ">",
            ],
            seconds_since_output=AUTH_TRANSIENT_GRACE_SEC,
        )
        pane = _pane("working", session=session, provider="gemini")
        result = Orchestrator._derive_display_state(None, pane, "working", True)
        assert result == "blocked:provider-account"

    def test_gemini_verifying_account_with_realistic_footer_is_still_blocked(self) -> None:
        # #363 regression: a realistic composer footer (border + status/hint
        # row) below the banner used to push it out of the 6-row
        # `_ready_region` window entirely, so `takkub status` never reported
        # `blocked:provider-account` for this exact frozen screen — see the
        # matching `test_fires_when_a_realistic_footer_pushes_the_banner_past_
        # ready_tail_rows` regression in test_auth_failure_detection.py for
        # the narrower unit-level proof.
        session = _RealSignalSession(
            [
                "⚠ Verifying your account...",
                "  We're finishing verifying your account eligibility.",
                "  This usually takes a moment. Please try again shortly.",
                "",
                "─" * 40,
                "> ",
                "─" * 40,
                "ctx: 12% used  |  tips: ctrl+c to exit",
                "? for shortcuts            Gemini 3.7 Flash (High)",
            ],
            seconds_since_output=AUTH_TRANSIENT_GRACE_SEC,
        )
        pane = _pane("working", session=session, provider="gemini")
        result = Orchestrator._derive_display_state(None, pane, "working", True)
        assert result == "blocked:provider-account"

    def test_gemini_verifying_account_during_normal_boot_is_not_blocked_yet(self) -> None:
        session = _RealSignalSession(
            ["⚠ Verifying your account...", "  Please try again shortly.", ">"],
            seconds_since_output=AUTH_TRANSIENT_GRACE_SEC - 1,
        )
        pane = _pane("working", session=session, provider="gemini")
        result = Orchestrator._derive_display_state(None, pane, "working", True)
        assert result != "blocked:provider-account"

    def test_gemini_stuck_signing_in_past_grace_is_login_required(self) -> None:
        session = _RealSignalSession(
            ["", "Signing in...", ""], seconds_since_output=AUTH_TRANSIENT_GRACE_SEC
        )
        pane = _pane("working", session=session, provider="gemini")
        result = Orchestrator._derive_display_state(None, pane, "working", True)
        assert result == "login-required"

    def test_gemini_signing_in_during_normal_boot_is_not_login_required_yet(self) -> None:
        session = _RealSignalSession(
            ["", "Signing in...", ""], seconds_since_output=AUTH_TRANSIENT_GRACE_SEC - 1
        )
        pane = _pane("working", session=session, provider="gemini")
        result = Orchestrator._derive_display_state(None, pane, "working", True)
        # Transient marker hasn't cleared its grace period yet — falls
        # through to the next tier (waiting-delivery, from the queued
        # delivery-health notice — #263's original evidence shape).
        assert result == "waiting-delivery"

    def test_kimi_stuck_needing_login_is_login_required(self) -> None:
        session = _RealSignalSession(
            ["", "Model: not set, send /login to login", ""], seconds_since_output=5.0
        )
        pane = _pane("active", session=session, provider="kimi")
        result = Orchestrator._derive_display_state(None, pane, "active", False)
        assert result == "login-required"

    def test_codex_mcp_cold_boot_is_booting(self) -> None:
        session = _RealSignalSession(
            ["OpenAI Codex (v0.145.0)", "booting mcp server..."], seconds_since_output=2.0
        )
        pane = _pane("active", session=session, provider="codex")
        result = Orchestrator._derive_display_state(None, pane, "active", False)
        assert result == "booting"

    def test_codex_working_with_a_queued_message_is_not_booting(self) -> None:
        """#281: codex shows "tab to queue message" the whole time it is
        working. Reading that as a boot phase made `takkub list` report
        "booting" for a pane that was actively running its task (and made the
        delivery watchdog warn about a stall that was not happening)."""
        session = _RealSignalSession(
            [
                "gpt-5.6 high",
                "Working (12s . esc to interrupt . tab to queue message)",
            ],
            seconds_since_output=0.0,
        )
        pane = _pane("working", session=session, provider="codex")
        result = Orchestrator._derive_display_state(None, pane, "working", False)
        assert result != "booting"

    def test_codex_genuinely_busy_with_no_dispatch_is_busy(self) -> None:
        # The issue's own evidence: `takkub list` said "active" while the
        # screen plainly showed the busy indicator, because no task had
        # ever been dispatched (pane.state stayed "active", never promoted
        # to "working").
        session = _RealSignalSession(
            ["gpt-5.5 medium", "Working (0s . esc to interrupt)"], seconds_since_output=0.0
        )
        pane = _pane("active", session=session, provider="codex")
        result = Orchestrator._derive_display_state(None, pane, "active", False)
        assert result == "busy"

    def test_gemini_footer_below_running_command_still_reads_stalled_tool(self) -> None:
        # #308's own incident shape: agy's idle footer ("? for shortcuts")
        # stayed visible on screen the whole time "Running command..." was
        # wedged above it — proves the tier fires from the real marker
        # method + real ready-region scoping, not just the abstract stub.
        session = _RealSignalSession(
            ["", "Running command...", "", "? for shortcuts   Gemini 3.1 Pro (high)"],
            seconds_since_output=TOOL_STUCK_TIMEOUT_SEC,
        )
        pane = _pane("working", session=session, provider="gemini")
        result = Orchestrator._derive_display_state(None, pane, "working", False)
        assert result == "stalled:tool"

    def test_cursor_uncalibrated_active_reads_unknown_not_active(self) -> None:
        session = _RealSignalSession(["cursor-agent", "> "], seconds_since_output=50.0)
        pane = _pane("active", session=session, provider="cursor")
        result = Orchestrator._derive_display_state(None, pane, "active", False)
        assert result == "unknown"


class TestListStatusDetailedWiring:
    """`list_status_detailed` must expose the new derivation as an
    additive `"display_state"` key while `"state"` itself stays
    byte-identical to before."""

    @pytest.fixture(scope="class")
    def qapp(self) -> QCoreApplication:
        app = QCoreApplication.instance()
        if app is None:
            app = QCoreApplication([])
        return app

    @pytest.fixture
    def orch(self, qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
        o = Orchestrator.__new__(Orchestrator)
        QObject.__init__(o)
        o._panes_by_project = {}
        o._pane_state = {}
        o._lead_digest_queue = {}
        o._lead_notify_queue = {}
        o._pending_done_notices = {}
        o._recent_done = []
        monkeypatch.setattr(o, "_resolve_project", lambda p=None: p or "display-state-wiring")
        monkeypatch.setattr(
            o, "_project_panes", lambda p=None: o._panes_by_project.get(o._resolve_project(p), {})
        )
        return o

    def test_display_state_added_without_changing_state(self, orch: Orchestrator) -> None:
        project = "display-state-wiring"
        session = _StubSession(auth_reason="send /login to login")
        pane = _pane("working", session=session, provider="kimi")
        orch._panes_by_project[project] = {"kimi": pane}
        orch._lead_digest_queue[project] = collections.deque()
        orch._lead_notify_queue[project] = collections.deque()

        detailed = orch.list_status_detailed(project=project)

        assert detailed["kimi"]["state"] == "working"
        assert detailed["kimi"]["display_state"] == "login-required"

    def test_display_state_falls_back_cleanly_when_every_signal_check_errors(
        self, orch: Orchestrator
    ) -> None:
        project = "display-state-wiring"
        session = _StubSession(raise_on=("auth", "booting", "busy"))
        pane = _pane("active", session=session, provider="claude")
        orch._panes_by_project[project] = {"backend": pane}
        orch._lead_digest_queue[project] = collections.deque()
        orch._lead_notify_queue[project] = collections.deque()

        detailed = orch.list_status_detailed(project=project)

        assert detailed["backend"]["state"] == "active"
        assert detailed["backend"]["display_state"] == "active"

    def test_queued_reassign_on_an_existing_pane_is_surfaced(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#412: a re-assign to an ALREADY-spawned pane that gets denied a
        resource-governor slot (e.g. heavy_project_limit) used to vanish —
        `_queued_resource_roles` only ever adds an entry for a role with NO
        pane at all yet, so the pane's stale previous state kept showing
        with nothing telling Lead a new task was parked behind a limit."""
        from agent_takkub.resource_governor import GovernorLimits, ResourceClass, ResourceGovernor

        project = "display-state-wiring"
        pane = _pane("active", session=None, provider="claude")
        orch._panes_by_project[project] = {"frontend": pane}
        orch._lead_digest_queue[project] = collections.deque()
        orch._lead_notify_queue[project] = collections.deque()
        governor = ResourceGovernor(GovernorLimits(max_heavy_per_project=2))
        orch._resource_governor = governor
        governor.request_slot(
            project_id=project,
            pane_id="backend",
            task_id="t-held",
            resource_class=ResourceClass.HEAVY,
        )
        governor.request_slot(
            project_id=project,
            pane_id="backend#2",
            task_id="t-held-2",
            resource_class=ResourceClass.HEAVY,
        )
        governor.enqueue(
            project_id=project,
            pane_id="frontend",
            task_id="t-wait",
            resource_class=ResourceClass.HEAVY,
            reason="heavy_project_limit",
        )

        detailed = orch.list_status_detailed(project=project)

        # The pane's own (unrelated) base state is untouched — only the
        # additive `display_state` tier changes, same additive-only contract
        # every other tier in this priority chain already follows.
        assert detailed["frontend"]["state"] == "spawning"
        assert detailed["frontend"]["display_state"] == "queued:heavy_project_limit"
        assert "heavy_project_limit" in (detailed["frontend"]["resource_wait_message"] or "")
