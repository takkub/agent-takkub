"""Tests for the boot-marker blind-paste guard (issue #271).

codex's `ready_wait_ms` (90s) routinely expired while the pane was still
showing its own boot-phase marker ("Booting MCP server: codex_apps …") —
measured 90-150s cold boot on real hardware, 4/4 trials 2026-08-16. When that
happened, `_send_when_ready`'s `_check()` fell through to a best-effort
"blind" paste (issue #26) straight into a composer that had not rendered
yet, so the bytes landed as raw keystrokes on the boot splash and the task
was lost outright rather than merely unconfirmed — 3 of 4 trials needed a
manual `takkub send` resend from Lead.

This adds a guard at the exact point that decision is made: while
`PtySession.shows_startup_marker()` is still true, `_check()` must keep
extending the wait (same shape as the #130/#144 busy-pane extension) instead
of blind-pasting, capped at `BUSY_WAIT_CEILING_SEC` so a boot that genuinely
never finishes still gets a last-resort delivery rather than polling
forever.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator


@pytest.fixture(autouse=True)
def _live_watch_notices(monkeypatch: pytest.MonkeyPatch) -> None:
    """#280 moved these watchdog observations out of immediate Lead notices
    and into the pane's own end-of-life report.

    This file tests the DETECTION — does the watchdog fire at the right
    moment, about the right role, with the right wording — none of which
    #280 changed; only the moment Lead is told did. `live` is the policy
    under which that message is still rendered verbatim, so every assertion
    below keeps testing exactly what it was written to test. The new routing
    has its own coverage in tests/test_pane_health_reporting.py and
    tests/test_pane_health_close_report.py.
    """
    monkeypatch.setenv("TAKKUB_PANE_WATCH_NOTICES", "live")


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
    s.is_at_trust_prompt.return_value = False
    s.is_blocked_on_tty_prompt.return_value = None
    s.is_blocked_on_permission_prompt.return_value = None
    s.shows_startup_marker.return_value = False
    s.shows_boot_phase_marker.return_value = False
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


def _written_strings(session: MagicMock) -> list[str]:
    return [
        c.args[0] for c in session.write.call_args_list if c.args and isinstance(c.args[0], str)
    ]


class TestBootMarkerBlindPasteGuard:
    def test_no_blind_paste_while_marker_visible_then_delivers_once_clear(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """Marker stays up well past max_wait_ms — no paste must land during
        that window. Once the marker clears and the pane reaches its ready
        prompt, the task must still be delivered normally (not flagged
        unconfirmed)."""
        lead = _pane(_live_session())
        codex = _pane(_live_session())
        poll = {"n": 0}

        def _ready(*_a, **_k) -> bool:
            poll["n"] += 1
            return poll["n"] > 20

        def _marker(*_a, **_k) -> bool:
            return poll["n"] <= 20

        codex.session.is_at_ready_prompt.side_effect = _ready
        codex.session.shows_boot_phase_marker.side_effect = _marker
        orch._panes_by_project["P"] = {"lead": lead, "codex": codex}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("codex", "run smoke", max_wait_ms=300, project="P")

        assert poll["n"] > 20, "poll loop must keep extending past the 20-poll boot window"
        warnings = _written_strings(lead.session)
        assert not any("[delivery-unconfirmed]" in m for m in warnings), (
            "must not blind-paste while the boot marker is still visible"
        )
        payload_writes = _written_strings(codex.session)
        assert payload_writes, "task must still be delivered once the pane reaches ready"

    def test_ceiling_fails_the_task_instead_of_blind_pasting(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """#276 changed this ceiling's verdict.

        The original #271 fix ended a never-finishing boot with a last-resort
        blind paste, on the reasoning that some delivery beats none. Live
        evidence said otherwise: the paste goes onto a splash screen with no
        composer, so it is lost exactly as this file's own docstring
        describes — the task was neither delivered nor failed, it simply
        stopped existing, and a report from an unrelated task later closed it
        out as done. The ceiling is now BOOT_STALL_CEILING_SEC and it fails
        the task explicitly instead: ledger flipped, Lead told, pane closed.
        """
        monkeypatch.setattr(orch_mod, "BOOT_STALL_CEILING_SEC", 1)  # 1s ceiling — fake clock
        lead = _pane(_live_session())
        codex = _pane(_live_session())
        codex.session.is_at_ready_prompt.return_value = False  # never ready
        codex.session.shows_boot_phase_marker.return_value = True  # marker never clears
        orch._panes_by_project["P"] = {"lead": lead, "codex": codex}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))
        failures: list[dict] = []
        monkeypatch.setattr(
            orch,
            "_fail_boot_stalled_delivery",
            lambda role, project, elapsed: failures.append({"role": role, "elapsed": elapsed}),
        )

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("codex", "run smoke", max_wait_ms=300, project="P")

        assert not _written_strings(codex.session), (
            "must never blind-paste onto a boot splash — that is how the task got lost"
        )
        assert len(failures) == 1, "the task must end as an explicit failure, not silence"
        assert failures[0]["role"] == "codex"

    def test_pre_spawn_wait_does_not_count_against_boot_marker_ceiling(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """#356: a role parked in the spawn gate's deferred set (or just slow
        to get an actual session under machine load — the same condition
        that gets a task resource-governor-queued in the first place) must
        not have that pre-spawn wait counted against `BOOT_STALL_CEILING_SEC`
        once its session finally comes up and shows a boot marker — that
        ceiling measures how long the pane has been stuck on ITS OWN boot
        marker, not how long delivery has been polling overall."""
        monkeypatch.setattr(orch_mod, "BOOT_STALL_CEILING_SEC", 1)  # 1s ceiling — fake clock
        lead = _pane(_live_session())
        codex = _pane(None)  # no session yet — simulates spawn-gate deferral
        orch._panes_by_project["P"] = {"lead": lead, "codex": codex}
        orch._spawn_deferred = {"P::codex"}

        live = _live_session()
        live.is_at_ready_prompt.return_value = False  # never ready
        live.shows_boot_phase_marker.return_value = True  # marker never clears
        calls = {"n": 0}

        def _fire(_ms, fn):
            calls["n"] += 1
            if calls["n"] == 20:
                # Session finally attaches after a long pre-spawn wait —
                # ~2.85s of "no session yet" ticks already accumulated.
                codex.session = live
            fn()

        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(_fire))
        failures: list[float] = []
        monkeypatch.setattr(
            orch,
            "_fail_boot_stalled_delivery",
            lambda role, project, elapsed: failures.append(elapsed),
        )

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("codex", "run smoke", max_wait_ms=300, project="P")

        assert calls["n"] > 20, "must have kept polling through the pre-spawn wait"
        assert len(failures) == 1
        # The 1s ceiling must be measured from when the session came alive
        # (~tick 20), not from when polling started — the old bug would trip
        # this the very first tick after the session appeared, reporting an
        # elapsed close to the full ~2.85s+ pre-spawn wait instead.
        assert failures[0] < 1.5, (
            f"pre-spawn wait leaked into the boot-marker ceiling: {failures[0]}"
        )

    def test_ready_pane_delivers_immediately_unaffected(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """A pane that is ready right away must be unaffected by this guard
        — no regression for the common #26/#144 path."""
        lead = _pane(_live_session())
        codex = _pane(_live_session())
        codex.session.is_at_ready_prompt.return_value = True
        orch._panes_by_project["P"] = {"lead": lead, "codex": codex}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("codex", "run smoke", max_wait_ms=1000, project="P")

        assert _written_strings(codex.session), "ready pane must still get its task delivered"
        assert not any("[delivery-unconfirmed]" in m for m in _written_strings(lead.session))

    def test_ceiling_reprobes_once_under_a_stale_heartbeat_then_delivers_if_marker_cleared(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """#387: `elapsed[0]` assumes ticks land every _READY_POLL_INTERVAL_MS,
        but a stalled Qt main thread delays the NEXT `_check()` call itself —
        by the time the ceiling branch finally runs, the boot-phase marker it
        is about to fail on may already be stale (the pane could have
        finished booting and repainted during the very backlog that delayed
        this poll from seeing it). Live evidence: `ready_marker_possibly_stale
        footer=""` immediately followed by `delivery_boot_timeout_failed
        elapsed 300s`, then a re-deliver accepted within 27s — the pane was
        ready, the read was not.

        A stale `_main_thread_heartbeat_age` (the same stall signal
        `_verify`'s own `_STALL_DEFER_AGE_S` guard already trusts) earns
        exactly one extra poll before paying the cost of failing the whole
        delivery. If the marker has genuinely cleared by then, deliver
        normally instead of failing on a stale read.
        """
        monkeypatch.setattr(orch_mod, "BOOT_STALL_CEILING_SEC", 0)  # trips on the first check
        monkeypatch.setattr(orch_mod, "_main_thread_heartbeat_age", lambda: 0.8)  # stale
        lead = _pane(_live_session())
        codex = _pane(_live_session())
        marker_calls = {"n": 0}

        def _marker(*_a, **_k) -> bool:
            marker_calls["n"] += 1
            return marker_calls["n"] <= 2  # still booting through the ceiling tick

        def _ready(*_a, **_k) -> bool:
            return marker_calls["n"] > 2  # ready from the reprobe tick onward

        codex.session.shows_boot_phase_marker.side_effect = _marker
        codex.session.is_at_ready_prompt.side_effect = _ready
        orch._panes_by_project["P"] = {"lead": lead, "codex": codex}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))
        failures: list[float] = []
        monkeypatch.setattr(
            orch,
            "_fail_boot_stalled_delivery",
            lambda role, project, elapsed: failures.append(elapsed),
        )
        log_spy = MagicMock()
        monkeypatch.setattr("agent_takkub.lead_inbox._log_event", log_spy)

        orch._send_when_ready("codex", "run smoke", max_wait_ms=300, project="P")

        assert failures == [], "one bounded re-probe must rescue a pane whose marker had cleared"
        assert _written_strings(codex.session), "task must still be delivered once ready"
        reprobe_events = [
            c
            for c in log_spy.call_args_list
            if c.args and c.args[0] == "task_deliver_boot_ceiling_reprobe"
        ]
        assert len(reprobe_events) == 1, "must re-probe exactly once, not loop"

    def test_ceiling_still_fails_after_one_reprobe_if_marker_never_clears(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """The re-probe is bounded — a machine that stays stalled (or a pane
        that is genuinely still stuck) past the extra tick must still fail
        out exactly once, not hang forever waiting for a marker that will
        never clear."""
        monkeypatch.setattr(orch_mod, "BOOT_STALL_CEILING_SEC", 0)
        monkeypatch.setattr(orch_mod, "_main_thread_heartbeat_age", lambda: 0.8)  # stale
        lead = _pane(_live_session())
        codex = _pane(_live_session())
        codex.session.is_at_ready_prompt.return_value = False  # never ready
        codex.session.shows_boot_phase_marker.return_value = True  # marker never clears
        orch._panes_by_project["P"] = {"lead": lead, "codex": codex}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))
        failures: list[float] = []
        monkeypatch.setattr(
            orch,
            "_fail_boot_stalled_delivery",
            lambda role, project, elapsed: failures.append(elapsed),
        )
        log_spy = MagicMock()
        monkeypatch.setattr("agent_takkub.lead_inbox._log_event", log_spy)

        orch._send_when_ready("codex", "run smoke", max_wait_ms=300, project="P")

        assert not _written_strings(codex.session)
        assert len(failures) == 1, "must still fail out exactly once, not hang forever"
        reprobe_events = [
            c
            for c in log_spy.call_args_list
            if c.args and c.args[0] == "task_deliver_boot_ceiling_reprobe"
        ]
        assert len(reprobe_events) == 1, "the re-probe budget is exactly one extra tick"

    def test_ceiling_reprobes_once_even_with_a_fresh_heartbeat(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """#448: a real incident hit the ceiling with a perfectly fresh
        heartbeat (`heartbeat_age≈0`, no stall active AT THAT INSTANT) even
        though a `main_thread_stall` had happened 49s earlier — the old gate
        (`heartbeat_age > _STALL_DEFER_AGE_S`) only caught a stall in
        progress right at the ceiling check, so `reprobed` stayed `false` and
        the pane was failed on a read that could just as easily have been
        stale. The re-probe must fire regardless of the live heartbeat
        reading — it is one cheap extra tick either way."""
        monkeypatch.setattr(orch_mod, "BOOT_STALL_CEILING_SEC", 0)  # trips on the first check
        monkeypatch.setattr(orch_mod, "_main_thread_heartbeat_age", lambda: 0.0)  # NOT stale
        lead = _pane(_live_session())
        codex = _pane(_live_session())
        marker_calls = {"n": 0}

        def _marker(*_a, **_k) -> bool:
            marker_calls["n"] += 1
            return marker_calls["n"] <= 2  # still booting through the ceiling tick

        def _ready(*_a, **_k) -> bool:
            return marker_calls["n"] > 2  # ready from the reprobe tick onward

        codex.session.shows_boot_phase_marker.side_effect = _marker
        codex.session.is_at_ready_prompt.side_effect = _ready
        orch._panes_by_project["P"] = {"lead": lead, "codex": codex}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))
        failures: list[float] = []
        monkeypatch.setattr(
            orch,
            "_fail_boot_stalled_delivery",
            lambda role, project, elapsed: failures.append(elapsed),
        )
        log_spy = MagicMock()
        monkeypatch.setattr("agent_takkub.lead_inbox._log_event", log_spy)

        orch._send_when_ready("codex", "run smoke", max_wait_ms=300, project="P")

        assert failures == [], "one bounded re-probe must rescue a pane even with a fresh heartbeat"
        assert _written_strings(codex.session), "task must still be delivered once ready"
        reprobe_events = [
            c
            for c in log_spy.call_args_list
            if c.args and c.args[0] == "task_deliver_boot_ceiling_reprobe"
        ]
        assert len(reprobe_events) == 1, "must re-probe exactly once, not loop"

    def test_ceiling_timeout_reports_reprobed_true_after_one_fresh_heartbeat_reprobe(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """Companion to the above: when the marker never clears, the eventual
        failure's `reprobed` field must read `true` — not `false` like the
        #448 incident — because the ceiling always spends its one re-probe
        before giving up, regardless of the live heartbeat reading."""
        monkeypatch.setattr(orch_mod, "BOOT_STALL_CEILING_SEC", 0)
        monkeypatch.setattr(orch_mod, "_main_thread_heartbeat_age", lambda: 0.0)  # NOT stale
        lead = _pane(_live_session())
        codex = _pane(_live_session())
        codex.session.is_at_ready_prompt.return_value = False  # never ready
        codex.session.shows_boot_phase_marker.return_value = True  # marker never clears
        orch._panes_by_project["P"] = {"lead": lead, "codex": codex}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))
        failures: list[float] = []
        monkeypatch.setattr(
            orch,
            "_fail_boot_stalled_delivery",
            lambda role, project, elapsed: failures.append(elapsed),
        )
        log_spy = MagicMock()
        monkeypatch.setattr("agent_takkub.lead_inbox._log_event", log_spy)

        orch._send_when_ready("codex", "run smoke", max_wait_ms=300, project="P")

        assert len(failures) == 1, "must still fail out exactly once, not hang forever"
        reprobe_events = [
            c
            for c in log_spy.call_args_list
            if c.args and c.args[0] == "task_deliver_boot_ceiling_reprobe"
        ]
        assert len(reprobe_events) == 1, "the re-probe budget is exactly one extra tick"
        timeout_events = [
            c
            for c in log_spy.call_args_list
            if c.args and c.args[0] == "task_deliver_boot_marker_ceiling_timeout"
        ]
        assert len(timeout_events) == 1
        assert timeout_events[0].kwargs["reprobed"] is True, (
            "the #448 incident logged reprobed=false here even though a stall had "
            "happened moments earlier — the re-probe must always be spent first"
        )

    def test_genuinely_stalled_pane_without_marker_still_blind_pastes(
        self, orch: Orchestrator, monkeypatch
    ) -> None:
        """A pane that is simply stuck (never shows the boot marker at all)
        must keep the pre-#271 #26 behaviour — this guard is scoped to the
        boot-marker case, not a general new stall tolerance."""
        lead = _pane(_live_session())
        codex = _pane(_live_session())
        codex.session.is_at_ready_prompt.return_value = False
        codex.session.shows_boot_phase_marker.return_value = False
        codex.session.seconds_since_output.return_value = float("inf")
        orch._panes_by_project["P"] = {"lead": lead, "codex": codex}
        monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

        with patch("agent_takkub.lead_inbox._log_event"):
            orch._send_when_ready("codex", "run smoke", max_wait_ms=300, project="P")

        warnings = _written_strings(lead.session)
        assert any("[delivery-unconfirmed]" in m and "#26" in m for m in warnings)
