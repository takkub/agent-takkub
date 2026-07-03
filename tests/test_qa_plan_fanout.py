"""Tests for QA plan-then-fan-out (--role qa --plan --shards N).

Covers the two-phase flow added on top of plain shard fan-out:
  - CLI: --plan requires --shards >= 2; sends ONE planner request (not N)
  - assign(plan=True): spawns a planner pane (shard_total=0), stores plan_fanout,
    wraps the task with planner instructions, creates NO shard group yet
  - _wrap_planner_task / _qa_plan_file shape
  - _fire_qa_plan_fanout: valid plan → N shard assigns carrying their bucket
  - _fire_qa_plan_fanout: missing/invalid plan → degraded self-split + Lead warn
  - _fire_qa_plan_fanout: plan with > N buckets → clamped to N
  - done() on a planner pane → fires the fan-out, suppresses the per-pane notice
"""

from __future__ import annotations

import argparse
import json
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub.orchestrator import Orchestrator, _exit_key

TEST_PROJECT = "plantest"


# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def orch(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or TEST_PROJECT),
    )
    o = Orchestrator()
    o._idle_watchdog.stop()
    # Run deferred (staggered) spawns inline so tests stay synchronous.
    monkeypatch.setattr(o, "_defer", lambda _delay, fn: fn())
    return o


def _make_pane(role_name: str = "qa") -> MagicMock:
    pane = MagicMock()
    pane.role = MagicMock()
    pane.role.name = role_name
    pane.state = "working"
    pane.session = MagicMock()
    pane.session.is_alive = True
    pane._session_cwd = "/project/web"
    pane._transcript_path = None
    return pane


# ──────────────────────────────────────────────────────────────
# CLI: --plan validation + single-request dispatch
# ──────────────────────────────────────────────────────────────


class TestCliPlanFlag:
    def _args(self, shards: int, plan: bool = True) -> argparse.Namespace:
        return argparse.Namespace(
            role="qa",
            shards=shards,
            plan=plan,
            auto_chain=False,
            cwd="/web",
            task="e2e ทั้งแอป",
            requires_commit=False,
        )

    def test_plan_requires_two_shards(self) -> None:
        from agent_takkub.cli import cmd_assign

        result = cmd_assign(self._args(shards=1))
        assert result["ok"] is False
        assert "shards" in result["msg"].lower()

    def test_plan_sends_single_request_not_n(self) -> None:
        """--plan --shards 4 must send ONE assign request carrying plan=True,
        not four per-shard requests (the orchestrator drives the fan-out)."""
        from agent_takkub.cli import cmd_assign

        with patch("agent_takkub.cli._request", return_value={"ok": True}) as req:
            result = cmd_assign(self._args(shards=4))

        assert result["ok"] is True
        assert req.call_count == 1
        payload = req.call_args.args[0]
        assert payload["plan"] is True
        assert payload["shard_total"] == 4
        assert payload["role"] == "qa"

    def test_plan_eight_accepted(self) -> None:
        from agent_takkub.cli import cmd_assign

        with patch("agent_takkub.cli._request", return_value={"ok": True}):
            assert cmd_assign(self._args(shards=8))["ok"] is True

    def test_no_plan_attr_defaults_off(self) -> None:
        """A Namespace lacking `plan` (older callers) must not crash."""
        from agent_takkub.cli import cmd_assign

        ns = argparse.Namespace(
            role="qa", shards=1, auto_chain=False, cwd=None, task="x", requires_commit=False
        )
        with patch("agent_takkub.cli._request", return_value={"ok": True}):
            assert cmd_assign(ns)["ok"] is True


# ──────────────────────────────────────────────────────────────
# assign(plan=True): planner pane setup
# ──────────────────────────────────────────────────────────────


class TestAssignPlan:
    def test_planner_stores_fanout_and_wraps_task(self, orch: Orchestrator) -> None:
        pane = _make_pane("qa")
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["qa"] = pane

        sent: dict = {}
        with (
            patch.object(orch, "spawn", return_value=(True, "spawned")) as sp,
            patch.object(
                orch,
                "_send_when_ready",
                side_effect=lambda role, task, project=None: sent.update(role=role, task=task),
            ),
        ):
            ok, _ = orch.assign(
                "qa", cwd="/web", task="smoke", shard_total=4, plan=True, project=TEST_PROJECT
            )

        assert ok is True
        # planner spawns as a NON-shard pane (shard_total 0)
        assert sp.call_args.kwargs.get("_shard_total") == 0
        # no shard group created at planner time
        assert f"{TEST_PROJECT}::qa" not in orch._shard_groups

        ek = _exit_key(TEST_PROJECT, "qa")
        ps = orch._pane_state[ek]
        assert ps.plan_fanout is not None
        assert ps.plan_fanout["shards"] == 4
        # The fan-out task carries the verify-fail reporting hint so the QA
        # shards inherit it (they verify → must report --fail on failure).
        assert ps.plan_fanout["task"].startswith("smoke")
        assert "done --fail" in ps.plan_fanout["task"]
        assert ps.plan_fanout["plan_file"].endswith(f"{TEST_PROJECT}-qa-plan.json")
        # wrapped planner task is what gets sent + remembered for respawn replay
        assert "QA PLANNER MODE" in sent["task"]
        assert "smoke" in sent["task"]
        assert ps.last_assigned_task == sent["task"]

    def test_wrap_planner_task_has_schema_and_path(self, orch: Orchestrator) -> None:
        import pathlib

        wrapped = orch._wrap_planner_task("base task", pathlib.Path("/tmp/p.json"), 3)
        assert "/tmp/p.json" in wrapped or "p.json" in wrapped
        assert '"shards"' in wrapped
        assert "3" in wrapped
        assert "base task" in wrapped

    def test_qa_plan_file_path_shape(self, orch: Orchestrator) -> None:
        p = orch._qa_plan_file(TEST_PROJECT, "qa")
        assert p.name == f"{TEST_PROJECT}-qa-plan.json"
        assert p.parent.name == "qa-plans"


# ──────────────────────────────────────────────────────────────
# _fire_qa_plan_fanout: valid plan → bucketed shard assigns
# ──────────────────────────────────────────────────────────────


class TestFireFanout:
    def _cfg(self, plan_file: str, shards: int = 3) -> dict:
        return {"shards": shards, "cwd": "/web", "task": "e2e", "plan_file": plan_file}

    def test_valid_plan_fires_bucketed_shards(self, orch: Orchestrator, tmp_path) -> None:
        plan = {
            "shards": [
                {"n": 1, "scope": "/login, /signup", "focus": "invalid creds"},
                {"n": 2, "scope": "/dashboard, /reports", "focus": "empty state"},
                {"n": 3, "scope": "/settings, /admin", "focus": "rbac"},
            ]
        }
        pf = tmp_path / "plan.json"
        pf.write_text(json.dumps(plan), encoding="utf-8")

        notes: list[str] = []
        with (
            patch.object(orch, "assign") as asg,
            patch.object(orch, "_notify_lead", side_effect=lambda ns, m, **k: notes.append(m)),
        ):
            orch._fire_qa_plan_fanout(TEST_PROJECT, "qa", self._cfg(str(pf), 3))

        # three shard assigns, each carrying its bucket scope
        assert asg.call_count == 3
        roles = [c.args[0] for c in asg.call_args_list]
        assert roles == ["qa#1", "qa#2", "qa#3"]
        for c in asg.call_args_list:
            assert c.kwargs["shard_total"] == 3
            assert "SHARD" in c.kwargs["task"]
        # scope text flows into the matching shard
        assert "/dashboard" in asg.call_args_list[1].kwargs["task"]
        # Lead gets the plan-ready summary
        assert any("qa plan ready" in n for n in notes)

    def test_more_buckets_than_requested_clamped(self, orch: Orchestrator, tmp_path) -> None:
        plan = {"shards": [{"n": i, "scope": f"/p{i}"} for i in range(1, 6)]}  # 5 buckets
        pf = tmp_path / "plan.json"
        pf.write_text(json.dumps(plan), encoding="utf-8")

        with (
            patch.object(orch, "assign") as asg,
            patch.object(orch, "_notify_lead"),
        ):
            orch._fire_qa_plan_fanout(TEST_PROJECT, "qa", self._cfg(str(pf), 3))

        assert asg.call_count == 3  # clamped to requested 3

    def test_missing_plan_degrades_to_self_split(self, orch: Orchestrator, tmp_path) -> None:
        pf = tmp_path / "nope.json"  # never written

        notes: list[str] = []
        with (
            patch.object(orch, "assign") as asg,
            patch.object(orch, "_notify_lead", side_effect=lambda ns, m, **k: notes.append(m)),
        ):
            orch._fire_qa_plan_fanout(TEST_PROJECT, "qa", self._cfg(str(pf), 4))

        # still fans out N shards (degraded), each with the raw base task
        assert asg.call_count == 4
        for c in asg.call_args_list:
            assert "SHARD" not in c.kwargs["task"]  # no bucket injected
            assert c.kwargs["task"] == "e2e"
        assert any("fallback" in n for n in notes)

    def test_invalid_json_degrades(self, orch: Orchestrator, tmp_path) -> None:
        pf = tmp_path / "bad.json"
        pf.write_text("{not json", encoding="utf-8")

        notes: list[str] = []
        with (
            patch.object(orch, "assign") as asg,
            patch.object(orch, "_notify_lead", side_effect=lambda ns, m, **k: notes.append(m)),
        ):
            orch._fire_qa_plan_fanout(TEST_PROJECT, "qa", self._cfg(str(pf), 2))

        assert asg.call_count == 2
        assert any("fallback" in n for n in notes)

    def test_empty_shards_list_degrades(self, orch: Orchestrator, tmp_path) -> None:
        pf = tmp_path / "empty.json"
        pf.write_text(json.dumps({"shards": []}), encoding="utf-8")

        with (
            patch.object(orch, "assign") as asg,
            patch.object(orch, "_notify_lead"),
        ):
            orch._fire_qa_plan_fanout(TEST_PROJECT, "qa", self._cfg(str(pf), 2))

        assert asg.call_count == 2  # degraded fallback


# ──────────────────────────────────────────────────────────────
# done() on a planner pane → fires fan-out, suppresses per-pane notice
# ──────────────────────────────────────────────────────────────


class TestPlannerDoneIntegration:
    def test_done_fires_fanout_and_suppresses_notice(self, orch: Orchestrator) -> None:
        pane = _make_pane("qa")
        lead = MagicMock()
        lead.session = MagicMock()
        lead.session.is_alive = True
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["qa"] = pane
        orch._panes_by_project[TEST_PROJECT]["lead"] = lead

        ek = _exit_key(TEST_PROJECT, "qa")
        cfg = {"shards": 2, "cwd": "/web", "task": "smoke", "plan_file": "/x/plan.json"}
        orch._ps(ek).plan_fanout = cfg

        with (
            patch("agent_takkub.orchestrator.Orchestrator._check_uncommitted_async"),
            patch.object(orch, "_fire_qa_plan_fanout") as fire,
            patch.object(orch, "_notify_lead") as notify,
        ):
            ok, _ = orch.done("qa", note="แบ่ง 2 buckets", project=TEST_PROJECT)

        assert ok is True
        fire.assert_called_once()
        # cfg threaded through to the fan-out
        assert fire.call_args.args[2] == cfg
        # planner's own "[qa done]" notice is suppressed (fan-out msg replaces it)
        assert notify.call_count == 0

    def test_done_non_planner_still_notifies(self, orch: Orchestrator) -> None:
        """A regular (non-planner, non-shard) done still notifies Lead."""
        pane = _make_pane("backend")
        lead = MagicMock()
        lead.session = MagicMock()
        lead.session.is_alive = True
        orch._panes_by_project.setdefault(TEST_PROJECT, {})["backend"] = pane
        orch._panes_by_project[TEST_PROJECT]["lead"] = lead

        with (
            patch("agent_takkub.orchestrator.Orchestrator._check_uncommitted_async"),
            patch.object(orch, "_notify_lead") as notify,
        ):
            orch.done("backend", note="done", project=TEST_PROJECT)

        assert notify.call_count == 1
