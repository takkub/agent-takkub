"""Targeted tests for #280 — watch quietly, report at the end.

The cockpit narrated every watchdog observation to Lead the moment it happened:
`[delivery-busy-wait]`, `[delivery-boot-stall]`, `[delivery-unconfirmed]`,
no-content retry/degrade, auth degrade. Each interrupts Lead with a status
update about a pane that is still running and usually finishes normally a
moment later — and by the time Lead reads it, it is typically already stale
(which is why `_revalidate_system_notice` had to be written at all). The Lead
pane's scrollback ended up mostly cockpit self-narration.

The watching itself stays: it recovers panes and fails tasks that can no longer
succeed. What changed is when Lead hears — observations accumulate per pane
lifecycle and ride along with that pane's report at `done` / `done --fail` /
`close`.

Two classes still fire immediately, because "report at the end" cannot reach
Lead for them: the ones where there will be no end (spawn-failed, spawn-stuck,
respawn-capped) and the ones where the pane is blocked until a human acts (auth
wall, interactive prompt).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub.orchestrator import Orchestrator
from agent_takkub.pane_health import PaneHealth, summarize, watch_policy

PROJECT = "panehealth"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _live_session() -> MagicMock:
    s = MagicMock()
    s.is_alive = True
    s.write = MagicMock(return_value=True)
    s.is_at_ready_prompt.return_value = True
    s.is_at_trust_prompt.return_value = False
    s.is_blocked_on_tty_prompt.return_value = None
    s.is_blocked_on_permission_prompt.return_value = None
    return s


def _pane(state: str = "working") -> MagicMock:
    p = MagicMock()
    p.state = state
    p.session = _live_session()
    return p


@pytest.fixture
def orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    o = Orchestrator.__new__(Orchestrator)
    QObject.__init__(o)
    o._panes_by_project = {PROJECT: {"lead": _pane(), "backend": _pane()}}
    monkeypatch.setattr(o, "_resolve_project", lambda p=None: p or PROJECT)
    monkeypatch.setattr(
        o, "_project_panes", lambda p=None: o._panes_by_project.get(o._resolve_project(p), {})
    )
    return o


def _notices(orch: Orchestrator, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(orch, "_notify_lead", lambda p, body, **kw: seen.append(body) or None)
    return seen


class TestSummaryModel:
    def test_nothing_observed_renders_nothing(self) -> None:
        assert summarize(None) == ""
        assert summarize(PaneHealth()) == ""

    def test_repeats_collapse_to_a_count(self) -> None:
        h = PaneHealth()
        for _ in range(11):
            h.record("delivery-unconfirmed", "task ถูก paste แบบ blind")
        line = summarize(h)
        assert "×11" in line, "eleven observations must be one clause, not eleven"
        assert line.count("blind") == 1

    def test_first_detail_per_kind_wins(self) -> None:
        """For the events carrying a number, the first is the threshold that
        tripped the watchdog — the informative one."""
        h = PaneHealth()
        h.record("boot-stall", "boot ช้า 110s")
        h.record("boot-stall", "boot ช้า 260s")
        assert "110s" in summarize(h)
        assert "260s" not in summarize(h)

    def test_distinct_kinds_are_all_kept(self) -> None:
        h = PaneHealth()
        h.record("boot-stall", "boot ช้า 110s")
        h.record("delivery-unconfirmed", "paste blind")
        line = summarize(h)
        assert "boot ช้า 110s" in line and "paste blind" in line

    def test_event_cap_bounds_a_retry_loop(self) -> None:
        from agent_takkub.pane_health import MAX_EVENTS_PER_PANE

        h = PaneHealth()
        for _ in range(MAX_EVENTS_PER_PANE + 25):
            h.record("delivery-busy-wait", "waiting")
        assert len(h.events) == MAX_EVENTS_PER_PANE

    def test_policy_defaults_to_terminal_and_ignores_garbage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TAKKUB_PANE_WATCH_NOTICES", raising=False)
        assert watch_policy() == "terminal"
        monkeypatch.setenv("TAKKUB_PANE_WATCH_NOTICES", "nonsense")
        assert watch_policy() == "terminal", "an unreadable policy must not silence anything"
        monkeypatch.setenv("TAKKUB_PANE_WATCH_NOTICES", "LIVE")
        assert watch_policy() == "live"


class TestWatchdogsStopNarrating:
    def test_busy_wait_is_held_for_the_report(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TAKKUB_PANE_WATCH_NOTICES", raising=False)
        seen = _notices(orch, monkeypatch)
        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_delivery_busy_wait("backend", PROJECT, 12.0)
        assert seen == [], "a delivery still in flight must not interrupt Lead"
        assert "delivery รอ ready prompt" in orch._drain_pane_health(PROJECT, "backend")

    def test_boot_stall_is_held_for_the_report(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TAKKUB_PANE_WATCH_NOTICES", raising=False)
        seen = _notices(orch, monkeypatch)
        monkeypatch.setattr(orch, "_run_boot_diagnostic_async", lambda *a, **k: None)
        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_delivery_boot_stall("backend", PROJECT, 110.0)
        assert seen == []
        assert "boot ช้า 110s" in orch._drain_pane_health(PROJECT, "backend")

    def test_unconfirmed_delivery_is_held_for_the_report(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TAKKUB_PANE_WATCH_NOTICES", raising=False)
        seen = _notices(orch, monkeypatch)
        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_delivery_unconfirmed("backend", PROJECT, 90_000)
        assert seen == []
        assert "paste แบบ blind" in orch._drain_pane_health(PROJECT, "backend")

    def test_live_policy_restores_the_old_behaviour(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TAKKUB_PANE_WATCH_NOTICES", "live")
        seen = _notices(orch, monkeypatch)
        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_delivery_busy_wait("backend", PROJECT, 12.0)
        assert seen and "[delivery-busy-wait]" in seen[0]

    def test_off_policy_records_nothing_at_all(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TAKKUB_PANE_WATCH_NOTICES", "off")
        seen = _notices(orch, monkeypatch)
        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_delivery_busy_wait("backend", PROJECT, 12.0)
        assert seen == []
        assert orch._drain_pane_health(PROJECT, "backend") == ""


class TestStillImmediate:
    """The cases a report-at-the-end can never reach."""

    def test_spawn_failed_still_fires_now(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _notices(orch, monkeypatch)
        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_spawn_failed("backend", PROJECT, "no pty")
        assert seen and "[spawn-failed]" in seen[0], "there will be no pane to report at close"

    def test_spawn_stuck_still_fires_now(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _notices(orch, monkeypatch)
        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_spawn_stuck("backend", PROJECT, 90.0)
        assert seen and "[spawn-stuck]" in seen[0]

    def test_blocked_prompt_still_fires_now(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _notices(orch, monkeypatch)
        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_delivery_blocked_prompt("backend", PROJECT, "trust")
        assert seen, "a pane waiting on a keypress stays blocked until someone acts"


class TestFoldedIntoTheRealReport:
    """End-to-end through `done()` — the observation must actually arrive
    attached to the report Lead reads, not merely be stored."""

    @pytest.fixture
    def done_orch(self, qapp, tmp_path, monkeypatch) -> Orchestrator:
        from agent_takkub import orchestrator as orch_mod

        monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
        monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
        monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)
        monkeypatch.setattr(orch_mod, "_resolve_vault_dir", lambda: None)
        monkeypatch.setattr(orch_mod, "active_project", lambda: (PROJECT, {}))
        with patch("agent_takkub.orchestrator.Orchestrator._load_pending_cc", lambda self: None):
            o = Orchestrator.__new__(Orchestrator)
            QObject.__init__(o)
            o._panes_by_project = {PROJECT: {"lead": _pane(), "backend": _pane()}}
            o._pane_state = {}
            o._idle_state = {}
            o._recent_exits = {}
            o._recent_done = []
            o._pending_lead_cc = {}
            o._pending_done_notices = {}
        monkeypatch.setattr(o, "_write_hot_md", MagicMock())
        return o

    def test_done_notice_carries_what_the_watchdogs_saw(
        self, done_orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TAKKUB_PANE_WATCH_NOTICES", raising=False)
        captured: list[str] = []
        with patch("agent_takkub.lead_inbox._log_event"):
            done_orch._warn_lead_delivery_unconfirmed("backend", PROJECT, 90_000)
        monkeypatch.setattr(
            done_orch, "_notify_lead", lambda ns, notice, **kw: captured.append(notice) or None
        )

        done_orch.done("backend", note="เสร็จแล้ว", project=PROJECT)

        assert captured, "done() must still report"
        assert "[backend done] เสร็จแล้ว" in captured[0]
        assert "🩺 [pane health]" in captured[0], "the observation must ride along with the report"
        assert "paste แบบ blind" in captured[0]

    def test_a_clean_pane_adds_no_health_line(
        self, done_orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[str] = []
        monkeypatch.setattr(
            done_orch, "_notify_lead", lambda ns, notice, **kw: captured.append(notice) or None
        )
        done_orch.done("backend", note="เสร็จแล้ว", project=PROJECT)
        assert captured == ["[backend done] เสร็จแล้ว"], "nothing observed ⇒ nothing added"

    def test_done_drains_so_the_next_pane_starts_clean(
        self, done_orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TAKKUB_PANE_WATCH_NOTICES", raising=False)
        with patch("agent_takkub.lead_inbox._log_event"):
            done_orch._warn_lead_delivery_unconfirmed("backend", PROJECT, 90_000)
        monkeypatch.setattr(done_orch, "_notify_lead", lambda *a, **kw: None)
        done_orch.done("backend", note="รอบแรก", project=PROJECT)
        assert done_orch._drain_pane_health(PROJECT, "backend") == ""


class TestReportAtTheEnd:
    def test_health_rides_along_with_a_close(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TAKKUB_PANE_WATCH_NOTICES", raising=False)
        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_delivery_busy_wait("backend", PROJECT, 12.0)
        line = orch._drain_pane_health(PROJECT, "backend")
        assert line.startswith("🩺 [pane health]")

    def test_draining_is_once_per_lifecycle(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A respawn under the same role must start clean, not inherit the
        previous pane's history."""
        monkeypatch.delenv("TAKKUB_PANE_WATCH_NOTICES", raising=False)
        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_delivery_busy_wait("backend", PROJECT, 12.0)
        assert orch._drain_pane_health(PROJECT, "backend")
        assert orch._drain_pane_health(PROJECT, "backend") == ""

    def test_health_is_per_role_not_shared(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TAKKUB_PANE_WATCH_NOTICES", raising=False)
        with patch("agent_takkub.lead_inbox._log_event"):
            orch._warn_lead_delivery_busy_wait("backend", PROJECT, 12.0)
        assert orch._drain_pane_health(PROJECT, "frontend") == ""
        assert orch._drain_pane_health(PROJECT, "backend") != ""
