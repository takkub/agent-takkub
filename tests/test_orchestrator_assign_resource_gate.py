"""Issue #303: a `takkub assign` blocked behind the resource governor's
admission queue used to be a frozen dead end — nothing on disk to edit,
`task cancel`/`task show` both failed on "no pane open", and it just had to
be waited out (up to an hour reported in the field). This covers the
orchestrator-level wiring for items 1 and 2 of that issue:

  1. `assign()`'s gate-blocked branch writes the ledger detail file (and a
     "queued" INDEX.md row) the moment the task is queued, and re-reads that
     file at admission time so a hand-edit made while queued is what ships.
  2. `takkub task cancel --role <r>` can cancel a task that's still parked
     in the governor's waiting list, with no pane at all yet.

(cli.py needs no changes for either — `takkub send`/`task cancel`/`task
show` already forward generically to the orchestrator over IPC; the fix is
entirely in `assign()`/`cancel_task_delivery()` and their supporting
modules.)
"""

from __future__ import annotations

import pathlib

import pytest
from PyQt6.QtCore import QCoreApplication, QObject

from agent_takkub import orchestrator as orch_mod
from agent_takkub import task_ledger
from agent_takkub.orchestrator import Orchestrator
from agent_takkub.resource_governor import GovernorLimits, ResourceClass, ResourceGovernor

PROJECT = "proj"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
    monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)
    # task_ledger imports its own RUNTIME_DIR binding — must point at the
    # SAME tmp_path so this test reads back what assign() actually wrote.
    monkeypatch.setattr(task_ledger, "RUNTIME_DIR", tmp_path)

    o = Orchestrator.__new__(Orchestrator)
    QObject.__init__(o)
    o._panes_by_project = {}
    o._pane_state = {}
    o._session_goals = {}
    o._resource_tokens = {}
    o._resource_governor = ResourceGovernor(
        GovernorLimits(
            max_heavy_global=4,
            max_heavy_per_project=2,
            max_browser_global=1,
            max_build_global=2,
            max_test_global=2,
            max_package_install_global=1,
            cpu_pause_percent=85,
            cpu_resume_percent=65,
            min_available_ram_percent=20,
            resume_ram_percent=25,
        )
    )
    return o


def _occupy_the_only_browser_slot(orch: Orchestrator, holder_pane: str = "qa#1") -> None:
    held = orch._resource_governor.request_slot(
        project_id=PROJECT,
        pane_id=holder_pane,
        task_id="held",
        resource_class=ResourceClass.BROWSER,
    )
    assert held.allowed


class TestQueuedLedgerFileWrittenAtEnqueueTime:
    def test_gate_blocked_assign_writes_a_queued_row_immediately(self, orch: Orchestrator) -> None:
        _occupy_the_only_browser_slot(orch)

        ok, msg = orch.assign("qa#2", "/api", "run e2e suite", project=PROJECT)

        assert ok is True
        assert "queued" in msg
        state = task_ledger.load_state(PROJECT)
        assert "qa#2" in state["open"], "the queued task must already have an open ledger row"
        ptr = state["open"]["qa#2"]
        row = state["groups"][0]["features"][0]["rows"][ptr["row_index"]]
        assert row["status"] == "queued"
        assert row["detail_rel"] is not None

        detail_path = task_ledger._ledger_dir(PROJECT) / row["detail_rel"]
        assert detail_path.exists(), "Lead must have a real file to read/edit while queued"
        assert "run e2e suite" in detail_path.read_text(encoding="utf-8")

        index_text = task_ledger.index_path(PROJECT).read_text(encoding="utf-8")
        assert "🕓 อยู่ในคิว" in index_text


class TestAdmissionRereadsEditedFile:
    def test_hand_edit_while_queued_is_what_ships_on_admission(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _occupy_the_only_browser_slot(orch)
        orch.assign("qa#2", "/api", "run e2e suite", project=PROJECT)

        state = task_ledger.load_state(PROJECT)
        ptr = state["open"]["qa#2"]
        row = state["groups"][0]["features"][0]["rows"][ptr["row_index"]]
        detail_path = task_ledger._ledger_dir(PROJECT) / row["detail_rel"]

        # Lead tightens a safety condition on the file while it's still
        # queued — the exact scenario reported in #303.
        edited = detail_path.read_text(encoding="utf-8").replace(
            "run e2e suite",
            "run e2e suite\nห้ามรัน rebuild ถ้า docker engine ยังไม่นิ่ง",
        )
        detail_path.write_text(edited, encoding="utf-8")

        captured: dict = {}

        def _fake_assign(role, cwd, task, **kwargs):
            captured["role"] = role
            captured["task"] = task
            return True, "ok"

        # `on_admitted`'s closure calls `self.assign(...)`, resolved via
        # normal attribute lookup — an instance-level override intercepts it.
        monkeypatch.setattr(orch, "assign", _fake_assign)

        waiting = orch._resource_governor._waiting[PROJECT][0]
        fake_token = orch._resource_governor.request_slot(
            project_id=PROJECT,
            pane_id="qa#3",
            task_id="unrelated",
            resource_class=ResourceClass.TEST,
        ).token
        waiting.on_admitted(fake_token)

        assert captured["role"] == "qa#2"
        assert "ห้ามรัน rebuild" in captured["task"]
        assert "run e2e suite" in captured["task"]

    def test_no_edit_falls_back_to_the_original_text(
        self, orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _occupy_the_only_browser_slot(orch)
        orch.assign("qa#2", "/api", "run e2e suite unedited", project=PROJECT)

        captured: dict = {}
        monkeypatch.setattr(
            orch, "assign", lambda role, cwd, task, **kw: captured.update(task=task) or (True, "ok")
        )
        waiting = orch._resource_governor._waiting[PROJECT][0]
        fake_token = orch._resource_governor.request_slot(
            project_id=PROJECT,
            pane_id="qa#3",
            task_id="unrelated",
            resource_class=ResourceClass.TEST,
        ).token
        waiting.on_admitted(fake_token)

        assert captured["task"] == "run e2e suite unedited"


class TestCancelQueuedResourceTask:
    def test_cancel_removes_from_governor_queue_and_closes_ledger_row(
        self, orch: Orchestrator
    ) -> None:
        _occupy_the_only_browser_slot(orch)
        orch.assign("qa#2", "/api", "run e2e suite", project=PROJECT)
        assert len(orch._resource_governor._waiting.get(PROJECT, [])) == 1

        ok, msg = orch.cancel_task_delivery("qa#2", project=PROJECT)

        assert ok is True
        assert "cancelled" in msg
        assert (
            PROJECT not in orch._resource_governor._waiting
            or not orch._resource_governor._waiting[PROJECT]
        )
        state = task_ledger.load_state(PROJECT)
        assert "qa#2" not in state["open"]
        row = state["groups"][0]["features"][0]["rows"][0]
        assert row["status"] == "closed"

    def test_cancel_unrelated_unknown_role_still_reports_unknown(self, orch: Orchestrator) -> None:
        ok, msg = orch.cancel_task_delivery("totally-not-a-role", project=PROJECT)
        assert ok is False
        assert "unknown role" in msg

    def test_cancel_known_role_with_nothing_queued_or_delivered(self, orch: Orchestrator) -> None:
        ok, msg = orch.cancel_task_delivery("backend", project=PROJECT)
        assert ok is False
        assert "no pane open" in msg
