"""Tests for the flag-gated fan-out queue (Queue Executor).

With TAKKUB_QUEUE_FANOUT set, a fresh teammate spawn that would exceed
machine_total_pane_cap() is parked on a per-project queue and replayed when a
pane frees a slot. Default OFF → spawn behaviour unchanged. See
docs/reviews/2026-06-30-queue-gap.md.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent_takkub import exec_mode
from agent_takkub.orchestrator import LEAD, Orchestrator


class _Pane:
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
        self.assign = MagicMock(return_value=(True, "ok"))

    def _resolve_project(self, project):
        return project if project else "default"

    def _project_panes(self, project):
        return self._panes_by_project.get(project, {})


@pytest.fixture(autouse=True)
def _patch_side_effects(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("agent_takkub.orchestrator._delayed_enter", lambda *a, **k: None)
    monkeypatch.setattr("agent_takkub.orchestrator._log_event", lambda *a, **k: None)


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAKKUB_QUEUE_FANOUT", "1")


def _cap(monkeypatch: pytest.MonkeyPatch, cap: int) -> None:
    monkeypatch.setattr(exec_mode, "machine_total_pane_cap", lambda: cap)


def _should_queue(fake, role="backend", project="p") -> bool:
    return Orchestrator._should_queue_assign(fake, role, project)  # type: ignore[arg-type]


class TestShouldQueue:
    def test_flag_off_never_queues(self, monkeypatch) -> None:
        # No TAKKUB_QUEUE_FANOUT → default behaviour, never queue even over cap.
        monkeypatch.delenv("TAKKUB_QUEUE_FANOUT", raising=False)
        _cap(monkeypatch, 1)
        fake = _FakeOrch()
        fake._panes_by_project["p"] = {LEAD.name: _Pane(), "frontend": _Pane()}
        assert _should_queue(fake) is False

    def test_queues_when_over_cap(self, monkeypatch) -> None:
        _enable(monkeypatch)
        _cap(monkeypatch, 1)
        fake = _FakeOrch()
        fake._panes_by_project["p"] = {LEAD.name: _Pane(), "frontend": _Pane()}
        # 1 active teammate == cap → a NEW pane would be over → queue it.
        assert _should_queue(fake) is True

    def test_not_queue_under_cap(self, monkeypatch) -> None:
        _enable(monkeypatch)
        _cap(monkeypatch, 4)
        fake = _FakeOrch()
        fake._panes_by_project["p"] = {LEAD.name: _Pane(), "frontend": _Pane()}
        assert _should_queue(fake) is False

    def test_lead_never_queued(self, monkeypatch) -> None:
        _enable(monkeypatch)
        _cap(monkeypatch, 1)
        fake = _FakeOrch()
        fake._panes_by_project["p"] = {LEAD.name: _Pane(), "frontend": _Pane()}
        assert _should_queue(fake, role=LEAD.name) is False

    def test_reassign_live_pane_not_queued(self, monkeypatch) -> None:
        _enable(monkeypatch)
        _cap(monkeypatch, 1)
        fake = _FakeOrch()
        # backend is already alive → re-assign spawns nothing → must not queue.
        fake._panes_by_project["p"] = {
            LEAD.name: _Pane(),
            "frontend": _Pane(),
            "backend": _Pane(alive=True),
        }
        assert _should_queue(fake, role="backend") is False


class TestEnqueue:
    def test_enqueue_parks_and_notifies(self, monkeypatch) -> None:
        _enable(monkeypatch)
        _cap(monkeypatch, 1)
        fake = _FakeOrch()
        lead = _Pane()
        fake._panes_by_project["p"] = {LEAD.name: lead, "frontend": _Pane()}

        ok, msg = Orchestrator._enqueue_assign(
            fake, "backend", "/cwd", "do it", False, True, 0, False, "p"
        )  # type: ignore[arg-type]

        assert ok is True
        assert "queued" in msg
        # Parked with all flags preserved.
        item = fake._fanout_queue["p"][0]
        assert item["role"] == "backend"
        assert item["auto_chain"] is True
        assert item["task"] == "do it"
        # Lead was told.
        lead.session.write.assert_called_once()
        assert "queued" in lead.session.write.call_args[0][0]


class TestDrain:
    def _drain(self, fake, project="p"):
        Orchestrator._drain_fanout_queue(fake, project)  # type: ignore[arg-type]

    def test_drain_spawns_when_slot_free(self, monkeypatch) -> None:
        _enable(monkeypatch)
        _cap(monkeypatch, 2)
        fake = _FakeOrch()
        # 1 active teammate (< cap 2) and one item queued → drain replays it.
        fake._panes_by_project["p"] = {LEAD.name: _Pane(), "frontend": _Pane()}
        Orchestrator._enqueue_assign(fake, "backend", "/cwd", "task-b", True, False, 0, False, "p")  # type: ignore[arg-type]

        self._drain(fake)

        fake.assign.assert_called_once()
        args, kwargs = fake.assign.call_args
        assert args[0] == "backend"
        assert kwargs["requires_commit"] is True
        assert len(fake._fanout_queue["p"]) == 0

    def test_drain_noop_when_still_full(self, monkeypatch) -> None:
        _enable(monkeypatch)
        _cap(monkeypatch, 1)
        fake = _FakeOrch()
        # 1 active teammate == cap → still full → leave the item queued.
        fake._panes_by_project["p"] = {LEAD.name: _Pane(), "frontend": _Pane()}
        Orchestrator._enqueue_assign(fake, "backend", "/cwd", "task-b", False, False, 0, False, "p")  # type: ignore[arg-type]

        self._drain(fake)

        fake.assign.assert_not_called()
        assert len(fake._fanout_queue["p"]) == 1

    def test_drain_noop_when_flag_off(self, monkeypatch) -> None:
        _enable(monkeypatch)
        _cap(monkeypatch, 2)
        fake = _FakeOrch()
        fake._panes_by_project["p"] = {LEAD.name: _Pane()}
        Orchestrator._enqueue_assign(fake, "backend", "/cwd", "task-b", False, False, 0, False, "p")  # type: ignore[arg-type]
        # Now turn the flag off — drain must not replay.
        monkeypatch.delenv("TAKKUB_QUEUE_FANOUT", raising=False)

        self._drain(fake)
        fake.assign.assert_not_called()

    def test_drain_empty_queue_is_safe(self, monkeypatch) -> None:
        _enable(monkeypatch)
        _cap(monkeypatch, 2)
        fake = _FakeOrch()
        fake._panes_by_project["p"] = {LEAD.name: _Pane()}
        self._drain(fake)  # no queue at all → no error
        fake.assign.assert_not_called()

    def test_drain_one_per_call(self, monkeypatch) -> None:
        """One freed slot drains exactly one item; the next close drains the next."""
        _enable(monkeypatch)
        _cap(monkeypatch, 5)
        fake = _FakeOrch()
        fake._panes_by_project["p"] = {LEAD.name: _Pane()}
        for i in range(3):
            Orchestrator._enqueue_assign(
                fake, f"backend#{i}", "/cwd", f"t{i}", False, False, 0, False, "p"
            )  # type: ignore[arg-type]

        self._drain(fake)
        assert fake.assign.call_count == 1
        assert len(fake._fanout_queue["p"]) == 2
