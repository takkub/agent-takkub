"""Issue #833: cross-flow plans and revision-bound completion evidence."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_takkub import task_ledger
from agent_takkub.impact_contract import (
    FLOWS,
    append_block,
    git_revision,
    plan_digest,
    plan_from_task,
)


def _plan() -> dict:
    return {
        "trigger": "seedance_only selected for a project with dialogue",
        "before": "external speech could be mixed during assembly",
        "after": "native speech reaches export without external TTS",
        "source_of_truth": "project.audio_mode",
        "upstream": ["project settings", "episode continuation"],
        "downstream": ["voice UI", "assembly", "shots QA", "export"],
        "checks": [
            {
                "id": "worker-calls",
                "flow": "worker",
                "expected": "zero external TTS calls and native voice retained",
                "method": "fake provider and inspect call ledger",
                "owner": "qa",
            },
            {
                "id": "export-audio",
                "flow": "export",
                "expected": "native voice present in exported file",
                "method": "inspect fixture export",
                "owner": "qa",
            },
            {
                "id": "live-voice",
                "flow": "qa",
                "expected": "provider voice sounds correct",
                "method": "optional paid live check",
                "owner": "lead",
                "required": False,
            },
        ],
        "not_applicable": {
            flow: "This worker-only change does not use this flow."
            for flow in FLOWS
            if flow not in {"worker", "export", "qa"}
        },
    }


def _evidence(plan: dict, task_id: str, revision: str) -> dict:
    return {
        "task_id": task_id,
        "plan_digest": plan_digest(plan),
        "revision": revision,
        "checks": [
            {
                "id": "worker-calls",
                "status": "pass",
                "evidence": "fake call ledger: TTS=0; native speech=true",
                "kind": "fake",
            },
            {
                "id": "export-audio",
                "status": "pass",
                "evidence": "fixture export has native speech stream",
                "kind": "mock",
            },
            {
                "id": "live-voice",
                "status": "pending",
                "limitation": "paid provider generation not run",
            },
        ],
    }


def _repo(path: Path) -> None:
    for args in (
        ("init",),
        ("config", "user.email", "test@example.test"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    (path / "app.txt").write_text("before", encoding="utf-8")
    subprocess.run(["git", "add", "app.txt"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=path, check=True, capture_output=True)


def test_seedance_mode_requires_complete_plan() -> None:
    task = "แก้ audio_mode seedance_only ให้ไม่มี external TTS ถึง export"
    plan, error = plan_from_task(task)
    assert plan is None and "required" in error
    incomplete = _plan()
    del incomplete["not_applicable"]["continuation"]
    _, error = plan_from_task(append_block(task, "impact-plan", incomplete))
    assert "continuation" in error
    plan, error = plan_from_task(append_block(task, "impact-plan", _plan()))
    assert not error and plan == _plan()


def test_ledger_rejects_stale_and_incomplete_evidence_after_resume(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo(repo)
    monkeypatch.setattr(task_ledger, "RUNTIME_DIR", tmp_path / "runtime")
    plan = _plan()
    task = append_block("แก้ audio_mode seedance_only", "impact-plan", plan)
    warning, detail = task_ledger.create_assignment(
        "project", "backend", str(repo), task, "goal", "feature", "codex", task_id="task-a"
    )
    assert not warning and detail is not None
    active = task_ledger.open_impact("project", "backend")
    assert active is not None and active["task_id"] == "task-a"
    assert active["impact"]["plan"] == plan

    revision = git_revision(repo)
    assert revision is not None
    evidence = _evidence(plan, "task-a", revision)
    assert "missing" in task_ledger.check_impact_completion("project", "backend", "task-a", "done")
    assert task_ledger.open_impact("project", "backend")["status"] == "working"

    old = dict(evidence, task_id="task-old")
    assert "different task" in task_ledger.check_impact_completion(
        "project", "backend", "task-a", append_block("done", "impact-evidence", old)
    )
    missing = dict(evidence, checks=evidence["checks"][:-1])
    assert "IDs" in task_ledger.check_impact_completion(
        "project", "backend", "task-a", append_block("done", "impact-evidence", missing)
    )
    (repo / "app.txt").write_text("after", encoding="utf-8")
    assert "revision" in task_ledger.check_impact_completion(
        "project", "backend", "task-a", append_block("done", "impact-evidence", evidence)
    )
    evidence["revision"] = git_revision(repo)
    assert not task_ledger.check_impact_completion(
        "project", "backend", "task-a", append_block("done", "impact-evidence", evidence)
    )
    persisted = task_ledger.load_state("project")
    row = persisted["groups"][0]["features"][0]["rows"][0]
    assert row["impact"]["evidence"] == evidence
    assert row["status"] == "working"  # completion gate precedes mark_done


def test_plan_and_gate_are_provider_independent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(task_ledger, "RUNTIME_DIR", tmp_path / "runtime")
    task = append_block("เปลี่ยนโหมดเสียง", "impact-plan", _plan())
    for role, provider in (("frontend", "gemini"), ("backend", "codex")):
        warning, _detail = task_ledger.create_assignment(
            "project", role, str(tmp_path), task, "goal", "feature", provider, task_id=role
        )
        assert not warning
        assert task_ledger.open_impact("project", role)["impact"]["plan_digest"] == plan_digest(
            _plan()
        )


def test_revision_tracks_modified_and_untracked_files(tmp_path: Path) -> None:
    _repo(tmp_path)
    first = git_revision(tmp_path)
    (tmp_path / "app.txt").write_text("changed", encoding="utf-8")
    second = git_revision(tmp_path)
    (tmp_path / "new.txt").write_text("new", encoding="utf-8")
    third = git_revision(tmp_path)
    assert first and second and third and len({first, second, third}) == 3


def test_assign_rejects_sensitive_task_before_backlog_or_spawn(monkeypatch) -> None:
    from agent_takkub.orchestrator import Orchestrator

    monkeypatch.setattr(Orchestrator, "_resolve_project", staticmethod(lambda project=None: "p"))
    orch = Orchestrator()
    orch.shutdown_timers()
    with (
        patch.object(orch, "activate_assign_backlog") as backlog,
        patch.object(orch, "spawn") as spawn,
    ):
        ok, message = orch.assign("backend", None, "แก้ audio_mode seedance_only", project="p")
    assert not ok and "impact plan required" in message
    backlog.assert_not_called()
    spawn.assert_not_called()


def test_rejected_done_keeps_pane_and_delivery_active(monkeypatch, tmp_path: Path) -> None:
    from agent_takkub.orchestrator import Orchestrator

    monkeypatch.setattr(Orchestrator, "_resolve_project", staticmethod(lambda project=None: "p"))
    orch = Orchestrator()
    orch.shutdown_timers()
    pane = MagicMock()
    pane.state = "working"
    pane.session.is_alive = True
    pane.model.provider_name = "codex"
    pane.session.shows_busy_queue_confirm.return_value = False
    pane._session_cwd = str(tmp_path)
    pane._transcript_path = None
    orch._panes_by_project.setdefault("p", {})["backend"] = pane
    ps = orch._ps("p::backend")
    ps.task_id = "impact-task"
    ps.last_assigned_task = "change audio_mode"
    ps.task_delivered = True
    orch._last_delivery_ids = {("p", "backend"): "delivery-1"}
    delivery = MagicMock()
    orch._delivery_manager = delivery
    with patch("agent_takkub.task_ledger.check_impact_completion", return_value="missing checks"):
        ok, message = orch.done("backend", note="tests passed", project="p")
    assert not ok and "missing checks" in message
    assert pane.state == "working"
    assert orch._last_delivery_ids[("p", "backend")] == "delivery-1"
    delivery.mark_done.assert_not_called()
