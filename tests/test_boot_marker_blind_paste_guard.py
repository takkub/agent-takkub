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
