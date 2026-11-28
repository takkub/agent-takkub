"""Issue #833: cross-flow plans and revision-bound completion evidence."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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


@pytest.mark.parametrize("provider", ["claude", "codex", "gemini-agy", "opencode", "cursor"])
def test_task_impact_stdout_is_one_json_object(
    tmp_path: Path, monkeypatch, capsys, provider: str
) -> None:
    from agent_takkub import cli

    monkeypatch.setattr(task_ledger, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setenv("TAKKUB_PROJECT", "impact-json")
    monkeypatch.setenv("TAKKUB_ROLE", "reviewer")
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo(repo)
    plan = _plan()
    warning, detail = task_ledger.create_assignment(
        "impact-json",
        "reviewer",
        str(repo),
        append_block("Verify current flow", "impact-plan", plan),
        "goal",
        "feature",
        provider,
        task_id="current-task",
    )
    assert not warning and detail is not None

    assert cli.main(["task", "impact", "--role", "reviewer"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["task_id"] == "current-task"
    assert payload["revision"] == git_revision(repo)
    assert payload["status"] == "working"
    assert payload["impact"]["plan_digest"] == plan_digest(plan)
    assert payload["impact"]["plan"]["checks"] == plan["checks"]
    assert captured.err == ""


def test_task_impact_no_active_task_keeps_stdout_empty(tmp_path, monkeypatch, capsys) -> None:
    from agent_takkub import cli

    monkeypatch.setattr(task_ledger, "RUNTIME_DIR", tmp_path)
    assert cli.main(["task", "impact", "--role", "reviewer", "--project", "empty"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no active task for reviewer in empty" in captured.err


def test_task_impact_read_failure_keeps_stdout_empty(monkeypatch, capsys) -> None:
    from agent_takkub import cli

    def unreadable(*_args):
        raise OSError("ledger unreadable")

    monkeypatch.setattr(task_ledger, "open_impact", unreadable)
    assert cli.main(["task", "impact", "--role", "reviewer"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ledger unreadable" in captured.err


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


def test_non_git_task_root_can_complete_without_a_faked_revision_1001(
    tmp_path: Path, monkeypatch
) -> None:
    """#1001: a multi-repo parent folder is no git tree — `task impact` gave
    revision null and the gate rejected every done, so finished work could
    never report. It now reports the explicit `no-git` marker, and evidence
    for such a root may carry it or null; a git root still pins the tree."""
    from agent_takkub.impact_contract import NO_GIT_REVISION, validate_evidence

    parent = tmp_path / "multi-repo-parent"
    parent.mkdir()
    if any((d / ".git").exists() for d in (parent.resolve(), *parent.resolve().parents)):
        pytest.skip("tmp dir sits inside a git tree on this machine")
    assert git_revision(parent) == NO_GIT_REVISION
    assert git_revision(tmp_path / "missing-dir") is None  # unreadable stays a reject
    monkeypatch.setattr(task_ledger, "RUNTIME_DIR", tmp_path / "runtime")
    plan = _plan()
    task = append_block("แก้ audio_mode seedance_only", "impact-plan", plan)
    task_ledger.create_assignment(
        "project", "devops", str(parent), task, "goal", "feature", "codex", task_id="task-n"
    )
    for rev in (None, NO_GIT_REVISION):
        note = append_block("done", "impact-evidence", _evidence(plan, "task-n", rev))
        assert not task_ledger.check_impact_completion("project", "devops", "task-n", note)
    assert "revision" in validate_evidence(
        plan, _evidence(plan, "task-n", NO_GIT_REVISION), task_id="task-n", revision="git:abc"
    )
    assert "missing: revision" in validate_evidence(
        plan, _evidence(plan, "task-n", None), task_id="task-n", revision="git:abc"
    )


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


def test_missing_durable_plan_blocks_completion(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(task_ledger, "RUNTIME_DIR", tmp_path / "runtime")
    task = append_block("แก้ audio_mode seedance_only", "impact-plan", _plan())
    assert not task_ledger.impact_plan_is_durable("project", "backend", "task-a", _plan())
    assert "missing from durable task ledger" in task_ledger.check_impact_completion(
        "project", "backend", "task-a", "done", task_text=task
    )
    task_ledger.create_assignment(
        "project", "backend", str(tmp_path), task, "goal", "feature", "codex", task_id="task-a"
    )
    assert task_ledger.impact_plan_is_durable("project", "backend", "task-a", _plan())
    assert not task_ledger.impact_plan_is_durable("project", "backend", "task-b", _plan())


def test_revision_tracks_modified_and_untracked_files(tmp_path: Path) -> None:
    _repo(tmp_path)
    first = git_revision(tmp_path)
    (tmp_path / "app.txt").write_text("changed", encoding="utf-8")
    second = git_revision(tmp_path)
    (tmp_path / "new.txt").write_text("new", encoding="utf-8")
    third = git_revision(tmp_path)
    assert first and second and third and len({first, second, third}) == 3


def _pre_1070_revision(root: Path) -> str:
    """The #833 single-repo algorithm, restated: #1070 must not change it."""
    import hashlib

    def run(*args: str) -> bytes:
        return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout

    digest = hashlib.sha256(run("rev-parse", "HEAD").strip())
    names = run("diff", "--name-only", "-z", "HEAD") + run(
        "ls-files", "--others", "--exclude-standard", "-z"
    )
    for raw in sorted(set(names.split(b"\0")) - {b""}):
        digest.update(raw + b"\0")
        path = root / raw.decode()
        digest.update(path.read_bytes() if path.is_file() else b"<deleted>")
    return "git:" + digest.hexdigest()[:20]


def _multi_parent(tmp_path: Path) -> Path:
    parent = tmp_path / "blueparking"
    for rel in ("pos", "engines/cloud-engine-master"):
        (parent / rel).mkdir(parents=True)
        _repo(parent / rel)
    if any((d / ".git").exists() for d in (parent.resolve(), *parent.resolve().parents)):
        pytest.skip("tmp dir sits inside a git tree on this machine")
    return parent


def test_single_repo_revision_unchanged_by_1070(tmp_path: Path) -> None:
    _repo(tmp_path)
    (tmp_path / "app.txt").write_text("changed", encoding="utf-8")
    (tmp_path / "new.txt").write_text("new", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    assert git_revision(tmp_path) == _pre_1070_revision(tmp_path)
    assert git_revision(tmp_path / "sub", "any-project") == _pre_1070_revision(tmp_path)


def test_multi_repo_parent_pins_every_sub_repo_1070(tmp_path: Path, monkeypatch) -> None:
    """#1070: a non-git parent with sub-repos gave `no-git`, so evidence was
    rejected every round. The revision now combines each sub-repo's, sorted
    by posix rel path (same on Windows/macOS), and moves when any one moves."""
    import hashlib

    parent = _multi_parent(tmp_path)
    (parent / "node_modules" / "dep").mkdir(parents=True)
    _repo(parent / "node_modules" / "dep")  # skipped: changes here never count
    first = git_revision(parent)
    expected = hashlib.sha256(b"repos\0")
    for rel in ("engines/cloud-engine-master", "pos"):
        expected.update(rel.encode() + b"\0" + git_revision(parent / rel).encode() + b"\0")
    assert first == "repos:" + expected.hexdigest()[:20]
    assert git_revision(parent) == first  # deterministic
    (parent / "node_modules" / "dep" / "app.txt").write_text("x", encoding="utf-8")
    assert git_revision(parent) == first

    monkeypatch.setattr(task_ledger, "RUNTIME_DIR", tmp_path / "runtime")
    plan = _plan()
    task = append_block("แก้ audio_mode seedance_only", "impact-plan", plan)
    task_ledger.create_assignment(
        "project", "backend", str(parent), task, "goal", "feature", "codex", task_id="task-m"
    )
    evidence = append_block("done", "impact-evidence", _evidence(plan, "task-m", first))
    (parent / "engines" / "cloud-engine-master" / "app.txt").write_text("y", encoding="utf-8")
    second = git_revision(parent)
    assert second != first
    assert "revision" in task_ledger.check_impact_completion(
        "project", "backend", "task-m", evidence
    )
    for stale in (None, "no-git"):  # the #1001 marker no longer fits a parent with repos
        stale_note = append_block("done", "impact-evidence", _evidence(plan, "task-m", stale))
        assert task_ledger.check_impact_completion("project", "backend", "task-m", stale_note)
    fresh = append_block("done", "impact-evidence", _evidence(plan, "task-m", second))
    assert not task_ledger.check_impact_completion("project", "backend", "task-m", fresh)


def test_registered_paths_win_over_the_scan_1070(tmp_path: Path, monkeypatch) -> None:
    from agent_takkub import config

    parent = _multi_parent(tmp_path)
    pos = parent / "pos"
    monkeypatch.setattr(
        config,
        "load_projects",
        lambda: {"projects": {"bp": {"paths": {"pos": str(pos / "src"), "x": str(tmp_path)}}}},
    )
    (pos / "src").mkdir()
    only_pos = git_revision(parent, "bp")
    assert only_pos.startswith("repos:") and only_pos != git_revision(parent)
    (parent / "engines" / "cloud-engine-master" / "app.txt").write_text("y", encoding="utf-8")
    assert git_revision(parent, "bp") == only_pos  # not registered → not pinned
    (pos / "app.txt").write_text("y", encoding="utf-8")
    assert git_revision(parent, "bp") != only_pos


def test_no_git_anywhere_names_where_it_looked_1070(tmp_path: Path) -> None:
    from agent_takkub.impact_contract import NO_GIT_REVISION, validate_evidence

    parent = tmp_path / "plain"
    (parent / "a" / "b" / "c" / "d").mkdir(parents=True)
    (parent / "a" / "b" / "c" / "d" / ".git").mkdir()  # deeper than the scan bound
    if any((d / ".git").exists() for d in (parent.resolve(), *parent.resolve().parents)):
        pytest.skip("tmp dir sits inside a git tree on this machine")
    assert git_revision(parent) == NO_GIT_REVISION
    plan = _plan()
    error = validate_evidence(
        plan, _evidence(plan, "t", "git:abc"), task_id="t", revision=NO_GIT_REVISION
    )
    assert "no git repo at the task cwd" in error and "depth 3" in error


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


def test_wrong_task_rejection_names_both_ids_845() -> None:
    from agent_takkub.impact_contract import validate_evidence

    plan = {"checks": [{"id": "a", "required": True}]}
    msg = validate_evidence(
        plan, {"task_id": "e05150e3-old"}, task_id="9a1b2c3d-new", revision="git:x"
    )
    assert "evidence e05150e3" in msg and "active 9a1b2c3d" in msg
    assert "takkub task impact" in msg


@pytest.mark.parametrize(
    "task",
    [
        # #869 live-run wave30: request-body literal + state description.
        "POST /exports with settings `audio_mode:'hybrid'` then poll until done",
        "export version 1 audio_mode native เลือกใช้อยู่",
        # #869 wave18: parenthetical context about another role's work.
        "แก้ label thumbnail (backend wave 18 สลับ identity ref เป็น panel หน้า)",
        # Lead's explicit opt-out with a reason.
        "switch identity ref label [no-impact: UI label only, no flow touched]",
    ],
)
def test_context_and_literals_do_not_require_plan(task: str) -> None:
    assert plan_from_task(task) == (None, "")


@pytest.mark.parametrize(
    "task",
    [
        "set `audio_mode` to hybrid for every export",
        "change audio_mode default to hybrid",
        "เปลี่ยนโหมดเสียงเป็น hybrid",
        # A bare marker without a reason is not an opt-out.
        "change audio_mode [no-impact: x]",
    ],
)
def test_real_mode_changes_still_require_plan_and_name_the_clause(task: str) -> None:
    plan, error = plan_from_task(task)
    assert plan is None and "required" in error
    assert "triggered by" in error
