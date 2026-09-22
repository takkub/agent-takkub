"""#696: the running cockpit notices a clobbered interpreter, repairs it,
tells Lead once, and holds the idle-reminder loop while it is broken."""

from __future__ import annotations

import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_takkub import orchestrator as orch_mod
from agent_takkub import venv_integrity
from agent_takkub.orchestrator import Orchestrator


def _fake_orch() -> SimpleNamespace:
    return SimpleNamespace(
        _panes_by_project={"proj": {}, "other": {}},
        _notify_lead=MagicMock(),
        _VENV_INTEGRITY_INTERVAL_S=Orchestrator._VENV_INTEGRITY_INTERVAL_S,
        idleReminderNotice=MagicMock(),
    )


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    logged: list[tuple[str, dict]] = []
    monkeypatch.setattr(orch_mod, "_log_event", lambda ev, **kw: logged.append((ev, kw)))
    return logged


def test_repairs_notifies_every_project_once_and_clears_flag(
    monkeypatch: pytest.MonkeyPatch, events: list
) -> None:
    py = pathlib.Path("C:/fake/venv/Scripts/python.exe")
    monkeypatch.setattr(
        venv_integrity, "find_problems", lambda executable=None: [(py, "too-small")]
    )
    monkeypatch.setattr(venv_integrity, "repair", lambda p: (True, f"restored {p}"))
    orch = _fake_orch()

    Orchestrator._maybe_check_venv_integrity(orch, 1000.0)

    assert [e for e, _ in events] == ["venv_python_clobbered", "venv_python_repaired"]
    assert orch._notify_lead.call_count == 2  # one per open project
    body = orch._notify_lead.call_args_list[0][0][1]
    assert "ซ่อมคืนแล้ว" in body
    assert orch._cli_interpreter_broken is False

    # Same incident again within the throttle window → no second check at all.
    Orchestrator._maybe_check_venv_integrity(orch, 1010.0)
    assert len(events) == 2
    # Past the throttle, still clobbered+repaired → logs, but Lead is not
    # told the same thing twice.
    Orchestrator._maybe_check_venv_integrity(orch, 1100.0)
    assert orch._notify_lead.call_count == 2


def test_failed_repair_sets_broken_flag_and_holds_idle_reminders(
    monkeypatch: pytest.MonkeyPatch, events: list
) -> None:
    py = pathlib.Path("/fake/venv/bin/python")
    monkeypatch.setattr(
        venv_integrity, "find_problems", lambda executable=None: [(py, "bad-magic")]
    )
    monkeypatch.setattr(venv_integrity, "repair", lambda p: (False, "no healthy source"))
    orch = _fake_orch()

    Orchestrator._maybe_check_venv_integrity(orch, 1000.0)

    assert orch._cli_interpreter_broken is True
    assert events[-1][0] == "venv_python_repair_failed"
    assert "ซ่อมอัตโนมัติไม่ได้" in orch._notify_lead.call_args[0][1]

    pane = SimpleNamespace(session=SimpleNamespace(is_alive=True, write=MagicMock()))
    Orchestrator._inject_idle_reminder(orch, "proj", "backend", pane, 1, escalate=True)
    orch.idleReminderNotice.emit.assert_not_called()
    pane.session.write.assert_not_called()
    assert events[-1] == (
        "idle_reminder_skipped",
        {"role": "backend", "project": "proj", "reason": "cli_interpreter_broken"},
    )

    # Healthy again → flag clears and reminders resume.
    monkeypatch.setattr(venv_integrity, "find_problems", lambda executable=None: [])
    Orchestrator._maybe_check_venv_integrity(orch, 2000.0)
    assert orch._cli_interpreter_broken is False
    assert events[-1][0] == "venv_python_healthy_again"


def test_healthy_interpreter_is_silent(monkeypatch: pytest.MonkeyPatch, events: list) -> None:
    monkeypatch.setattr(venv_integrity, "find_problems", lambda executable=None: [])
    orch = _fake_orch()
    Orchestrator._maybe_check_venv_integrity(orch, 1000.0)
    assert events == []
    orch._notify_lead.assert_not_called()
