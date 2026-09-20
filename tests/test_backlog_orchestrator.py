"""#684: orchestrator.backlog_command dispatch + assign linkage + dialog logic."""

from __future__ import annotations

import pathlib

import pytest

from agent_takkub import backlog
from agent_takkub.backlog_dialog import ordered_selection


@pytest.fixture
def runtime(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    monkeypatch.setattr(backlog, "RUNTIME_DIR", tmp_path / "runtime")
    return tmp_path


class _FakeOrch:
    """Minimal orchestrator carrying the real backlog_command + a stubbed
    assign, enough to exercise the dispatch and assign linkage."""

    def __init__(self) -> None:
        from agent_takkub.orchestrator import Orchestrator

        self.backlog_command = Orchestrator.backlog_command.__get__(self)
        self._backlog_assign = Orchestrator._backlog_assign.__get__(self)
        self._compose_backlog_task = Orchestrator._compose_backlog_task
        self._render_backlog_detail = Orchestrator._render_backlog_detail
        self._backlog_simple = Orchestrator._backlog_simple
        self._backlog_result = Orchestrator._backlog_result
        self.assign_calls: list[dict] = []
        self._pane_state: dict = {}

    @staticmethod
    def _resolve_project(project):
        return project or "default"

    def assign(self, role, cwd, task, project=None, feature=""):
        from agent_takkub.orchestrator import _exit_key
        from agent_takkub.spawn_engine import PaneState

        self.assign_calls.append(
            {"role": role, "task": task, "project": project, "feature": feature}
        )
        ps = PaneState()
        ps.task_id = f"task-{len(self.assign_calls)}"
        self._pane_state[_exit_key(project or "default", role)] = ps
        return True, f"assigned to {role}"


class TestBacklogCommand:
    def test_add_list_roundtrip(self, runtime) -> None:
        orch = _FakeOrch()
        ok, _msg, payload = orch.backlog_command(
            "add", {"title": "ทดสอบ", "impact": "customer", "severity": "high"}, project="p"
        )
        assert ok
        item_id = payload["id"]
        ok2, _m2, p2 = orch.backlog_command("list", {"status": "open"}, project="p")
        assert ok2
        assert any(item_id in ln for ln in p2["lines"])
        assert p2["total"] == 1

    def test_block_needs_reason(self, runtime) -> None:
        orch = _FakeOrch()
        _ok, _m, payload = orch.backlog_command("add", {"title": "x"}, project="p")
        ok, msg, _ = orch.backlog_command("block", {"id": payload["id"], "reason": ""}, project="p")
        assert not ok and "เหตุผล" in msg

    def test_assign_fires_assign_and_links_ledger(self, runtime) -> None:
        orch = _FakeOrch()
        _ok, _m, payload = orch.backlog_command(
            "add",
            {"title": "แก้ช่องถอนเงิน", "source": "audit#H-09", "files": ["a.tsx:50"]},
            project="p",
        )
        item_id = payload["id"]
        ok, _msg, _p = orch.backlog_command(
            "assign", {"id": item_id, "role": "frontend"}, project="p"
        )
        assert ok
        assert len(orch.assign_calls) == 1
        call = orch.assign_calls[0]
        assert call["role"] == "frontend"
        assert "แก้ช่องถอนเงิน" in call["task"]
        assert "audit#H-09" in call["task"]  # source carried into the task spec
        # item flipped to doing + linked to the ledger task id
        item = backlog.get_item("p", item_id)
        assert item["status"] == "doing"
        assert item["ledger_task_id"] == "task-1"

    def test_assign_missing_role(self, runtime) -> None:
        orch = _FakeOrch()
        _ok, _m, payload = orch.backlog_command("add", {"title": "x"}, project="p")
        ok, msg, _ = orch.backlog_command("assign", {"id": payload["id"], "role": ""}, project="p")
        assert not ok and "role" in msg


class TestOrderedSelection:
    def test_preserves_click_order(self) -> None:
        items = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        # picked c then a — assign must run c, then a
        result = ordered_selection(["c", "a"], items)
        assert [it["id"] for it in result] == ["c", "a"]

    def test_skips_unknown_ids(self) -> None:
        items = [{"id": "a"}]
        assert ordered_selection(["a", "gone"], items) == [{"id": "a"}]

    def test_empty(self) -> None:
        assert ordered_selection([], [{"id": "a"}]) == []
