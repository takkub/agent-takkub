"""Tests for the over-capacity advisory (Queue-gap audit).

`_warn_lead_over_cap` warns the Lead when a fresh teammate spawn pushes the
TOTAL live pane count (across all projects) over `machine_total_pane_cap()`.
Non-blocking; rate-limited; Lead panes are excluded from the count and never
trigger it. See docs/reviews/2026-06-30-queue-gap.md.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent_takkub import exec_mode
from agent_takkub.orchestrator import LEAD, OVERCAP_WARN_COOLDOWN_S, Orchestrator


class _Pane:
    """Minimal pane: alive teammate (counts) or dead (doesn't)."""

    def __init__(self, alive: bool = True) -> None:
        if alive:
            sess = MagicMock()
            sess.is_alive = True
            self.session = sess
        else:
            self.session = None


class _FakeOrch:
    def __init__(self) -> None:
        self._panes_by_project: dict = {}
        self.leadInjected = MagicMock()
        self._last_overcap_warn_ts = 0.0

    def _project_panes(self, project: str) -> dict:
        return self._panes_by_project.get(project, {})


def _warn(fake: _FakeOrch, role: str = "backend", project: str = "p") -> None:
    Orchestrator._warn_lead_over_cap(fake, role, project)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _patch_side_effects(monkeypatch: pytest.MonkeyPatch):
    # Don't touch QTimer/Enter or the events log in these unit tests.
    monkeypatch.setattr("agent_takkub.orchestrator._delayed_enter", lambda *a, **k: None)
    monkeypatch.setattr("agent_takkub.orchestrator._log_event", lambda *a, **k: None)


def _set_cap(monkeypatch: pytest.MonkeyPatch, cap: int) -> None:
    monkeypatch.setattr(exec_mode, "machine_total_pane_cap", lambda: cap)


class TestOverCapacityWarn:
    def test_warns_when_total_at_or_over_cap(self, monkeypatch) -> None:
        _set_cap(monkeypatch, 2)
        fake = _FakeOrch()
        lead = _Pane()
        # 2 alive teammates already (across projects) == cap → the fresh spawn is over.
        fake._panes_by_project["p"] = {LEAD.name: lead, "frontend": _Pane()}
        fake._panes_by_project["q"] = {"backend": _Pane()}

        _warn(fake)

        assert lead.session.write.call_count == 1
        msg = lead.session.write.call_args[0][0]
        assert "over-capacity" in msg
        fake.leadInjected.emit.assert_called_once()

    def test_no_warn_under_cap(self, monkeypatch) -> None:
        _set_cap(monkeypatch, 4)
        fake = _FakeOrch()
        lead = _Pane()
        fake._panes_by_project["p"] = {LEAD.name: lead, "frontend": _Pane()}

        _warn(fake)

        lead.session.write.assert_not_called()
        fake.leadInjected.emit.assert_not_called()

    def test_lead_role_is_exempt(self, monkeypatch) -> None:
        _set_cap(monkeypatch, 1)
        fake = _FakeOrch()
        lead = _Pane()
        fake._panes_by_project["p"] = {LEAD.name: lead, "frontend": _Pane()}

        # Spawning a Lead pane is never an over-capacity event.
        _warn(fake, role=LEAD.name)

        lead.session.write.assert_not_called()

    def test_lead_panes_excluded_from_count(self, monkeypatch) -> None:
        _set_cap(monkeypatch, 2)
        fake = _FakeOrch()
        lead_p = _Pane()
        lead_q = _Pane()
        # Two Lead panes + one teammate → active teammates = 1 < cap 2 → no warn.
        fake._panes_by_project["p"] = {LEAD.name: lead_p, "frontend": _Pane()}
        fake._panes_by_project["q"] = {LEAD.name: lead_q}

        _warn(fake)
        lead_p.session.write.assert_not_called()

    def test_dead_panes_not_counted(self, monkeypatch) -> None:
        _set_cap(monkeypatch, 2)
        fake = _FakeOrch()
        lead = _Pane()
        # 1 alive + 1 dead teammate → active = 1 < cap 2 → no warn.
        fake._panes_by_project["p"] = {
            LEAD.name: lead,
            "frontend": _Pane(alive=True),
            "backend": _Pane(alive=False),
        }

        _warn(fake)
        lead.session.write.assert_not_called()

    def test_cooldown_suppresses_repeat(self, monkeypatch) -> None:
        _set_cap(monkeypatch, 1)
        fake = _FakeOrch()
        lead = _Pane()
        fake._panes_by_project["p"] = {LEAD.name: lead, "frontend": _Pane()}

        _warn(fake)
        assert lead.session.write.call_count == 1
        # Immediate repeat — within OVERCAP_WARN_COOLDOWN_S → suppressed.
        _warn(fake)
        assert lead.session.write.call_count == 1

    def test_warns_again_after_cooldown(self, monkeypatch) -> None:
        _set_cap(monkeypatch, 1)
        fake = _FakeOrch()
        lead = _Pane()
        fake._panes_by_project["p"] = {LEAD.name: lead, "frontend": _Pane()}

        _warn(fake)
        assert lead.session.write.call_count == 1
        # Backdate the last-warn so the cooldown has elapsed.
        fake._last_overcap_warn_ts -= OVERCAP_WARN_COOLDOWN_S + 1
        _warn(fake)
        assert lead.session.write.call_count == 2

    def test_no_lead_pane_is_safe(self, monkeypatch) -> None:
        _set_cap(monkeypatch, 1)
        fake = _FakeOrch()
        # Over cap but the project has no Lead pane → must not raise.
        fake._panes_by_project["p"] = {"frontend": _Pane()}
        _warn(fake)  # no exception
        fake.leadInjected.emit.assert_not_called()


class TestMachineTotalPaneCap:
    def test_floor_is_one(self) -> None:
        assert exec_mode.machine_total_pane_cap() >= 1

    def test_not_ceilinged_at_max_fanout(self, monkeypatch) -> None:
        """Unlike machine_fanout_cap(), the total cap is NOT bounded by MAX_FANOUT
        — a big box can run more than MAX_FANOUT panes in total."""
        monkeypatch.setattr(exec_mode.os, "cpu_count", lambda: 64)
        fake_vm = MagicMock()
        fake_vm.available = 256 * (1024**3)  # 256 GB free
        import psutil

        monkeypatch.setattr(psutil, "virtual_memory", lambda: fake_vm)
        cap = exec_mode.machine_total_pane_cap()
        assert cap > exec_mode.MAX_FANOUT  # 32 (cpu) vs MAX_FANOUT 4

    def test_tight_resources_floor(self, monkeypatch) -> None:
        monkeypatch.setattr(exec_mode.os, "cpu_count", lambda: 1)
        fake_vm = MagicMock()
        fake_vm.available = 1 * (1024**3)  # 1 GB free
        import psutil

        monkeypatch.setattr(psutil, "virtual_memory", lambda: fake_vm)
        assert exec_mode.machine_total_pane_cap() == 1
