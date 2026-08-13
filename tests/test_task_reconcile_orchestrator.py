"""Issue #166: Orchestrator.task_reconcile / task_close_role — the wiring
that supplies task_ledger's safety gate with the one thing it can't know on
its own, the set of roles with a currently-alive pane. `_orphan_candidates`'s
date<today gate is unit-tested directly in tests/test_task_ledger.py; these
tests cover the orchestrator plumbing (live-pane detection + IPC-facing
dry-run previews) on top of it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import orchestrator as orch_mod
from agent_takkub import task_ledger
from agent_takkub.orchestrator import Orchestrator

PROJECT = "p"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def _make_alive_session() -> MagicMock:
    s = MagicMock()
    s.is_alive = True
    return s


def _make_pane(session=None) -> MagicMock:
    p = MagicMock()
    p.session = session
    return p


@pytest.fixture
def orch(qapp, tmp_path, monkeypatch) -> Orchestrator:
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(orch_mod, "EVENTS_LOG", tmp_path / "events.log")
    monkeypatch.setattr(orch_mod, "ensure_runtime", lambda: None)
    monkeypatch.setattr(task_ledger, "RUNTIME_DIR", tmp_path)

    with (
        patch.object(Orchestrator, "_start_hot_md_timer", lambda self: None, create=True),
        patch("agent_takkub.orchestrator.Orchestrator._load_pending_cc", lambda self: None),
        patch(
            "agent_takkub.orchestrator.Orchestrator._start_browser_mcps",
            lambda self: None,
            create=True,
        ),
    ):
        o = Orchestrator.__new__(Orchestrator)
        from PyQt6.QtCore import QObject

        QObject.__init__(o)
        o._panes_by_project = {}
        o._pane_state = {}
        o._idle_state = {}
        o._recent_exits = {}
        o._recent_done = []
        o._pending_lead_cc = {}
    return o


def _backdate(role: str) -> None:
    state = task_ledger._load_state(PROJECT)
    ptr = state["open"][role]
    group = task_ledger._find_group(state, ptr["date"], ptr["goal"])
    group["date"] = "2020-01-01"
    ptr["date"] = "2020-01-01"
    task_ledger._save_state(PROJECT, state)


class TestTaskReconcile:
    def test_no_orphans_is_a_clean_no_op(self, orch: Orchestrator) -> None:
        ok, msg = orch.task_reconcile(project=PROJECT)
        assert ok is True
        assert "no orphaned rows" in msg

    def test_dry_run_previews_without_mutating(self, orch: Orchestrator) -> None:
        task_ledger.create_assignment(PROJECT, "backend", "/api", "do X", "g", "f", "claude")
        _backdate("backend")

        ok, msg = orch.task_reconcile(project=PROJECT, dry_run=True)

        assert ok is True
        assert "backend" in msg
        state = task_ledger._load_state(PROJECT)
        assert "backend" in state["open"]  # dry-run never mutates

    def test_closes_orphaned_row_when_role_has_no_live_pane(self, orch: Orchestrator) -> None:
        task_ledger.create_assignment(PROJECT, "backend", "/api", "do X", "g", "f", "claude")
        _backdate("backend")
        # backend pane widget doesn't exist in _panes_by_project — the #164
        # scenario (closed pane) is exactly the #166 orphan scenario.

        ok, msg = orch.task_reconcile(project=PROJECT)

        assert ok is True
        assert "backend" in msg
        state = task_ledger._load_state(PROJECT)
        assert "backend" not in state["open"]

    def test_never_closes_a_role_with_a_currently_live_pane(self, orch: Orchestrator) -> None:
        task_ledger.create_assignment(PROJECT, "backend", "/api", "do X", "g", "f", "claude")
        _backdate("backend")
        orch._panes_by_project.setdefault(PROJECT, {})["backend"] = _make_pane(
            session=_make_alive_session()
        )

        ok, msg = orch.task_reconcile(project=PROJECT)

        assert ok is True
        assert "no orphaned rows" in msg
        state = task_ledger._load_state(PROJECT)
        assert "backend" in state["open"]


class TestTaskCloseRole:
    def test_closes_role_with_no_live_pane(self, orch: Orchestrator) -> None:
        task_ledger.create_assignment(PROJECT, "qa", "/api", "smoke", "g", "f", "claude")

        ok, _msg = orch.task_close_role("qa", project=PROJECT)

        assert ok is True
        state = task_ledger._load_state(PROJECT)
        assert "qa" not in state["open"]

    def test_refuses_role_with_live_pane_unless_forced(self, orch: Orchestrator) -> None:
        task_ledger.create_assignment(PROJECT, "qa", "/api", "smoke", "g", "f", "claude")
        orch._panes_by_project.setdefault(PROJECT, {})["qa"] = _make_pane(
            session=_make_alive_session()
        )

        ok, msg = orch.task_close_role("qa", project=PROJECT)
        assert ok is False
        assert "live pane" in msg
        state = task_ledger._load_state(PROJECT)
        assert "qa" in state["open"]

        ok, msg = orch.task_close_role("qa", project=PROJECT, force=True)
        assert ok is True
        state = task_ledger._load_state(PROJECT)
        assert "qa" not in state["open"]

    def test_dry_run_previews_without_mutating(self, orch: Orchestrator) -> None:
        task_ledger.create_assignment(PROJECT, "qa", "/api", "smoke", "g", "f", "claude")

        ok, msg = orch.task_close_role("qa", project=PROJECT, dry_run=True)

        assert ok is True
        assert "qa" in msg
        state = task_ledger._load_state(PROJECT)
        assert "qa" in state["open"]  # dry-run never mutates

    def test_dry_run_reports_live_pane_block(self, orch: Orchestrator) -> None:
        task_ledger.create_assignment(PROJECT, "qa", "/api", "smoke", "g", "f", "claude")
        orch._panes_by_project.setdefault(PROJECT, {})["qa"] = _make_pane(
            session=_make_alive_session()
        )

        ok, msg = orch.task_close_role("qa", project=PROJECT, dry_run=True)

        assert ok is False
        assert "live pane" in msg
