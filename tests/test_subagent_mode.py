"""Targeted coverage for issue #268 native-subagent registration/completion."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import orchestrator as orchestrator_module
from agent_takkub import orchestrator_text, task_ledger
from agent_takkub.orchestrator import Orchestrator

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture
def orch(qapp: QCoreApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(Orchestrator, "_resolve_project", staticmethod(lambda p: p or "subtest"))
    monkeypatch.setattr(orchestrator_module, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(orchestrator_text, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(task_ledger, "RUNTIME_DIR", tmp_path)
    o = Orchestrator()
    o._idle_watchdog.stop()
    o._notify_lead = MagicMock()
    o._write_hot_md = MagicMock()
    return o


def test_subagent_assign_creates_capsule_without_pane(orch: Orchestrator, tmp_path: Path) -> None:
    ok, message = orch.assign(
        "reviewer", str(tmp_path), "audit auth defaults", mode="subagent", project="subtest"
    )

    assert ok is True
    assert "no pane" in message
    assert orch._project_panes("subtest").get("reviewer") is None
    state = orch._subagent_assignments[("subtest", "reviewer")]
    capsule = Path(state["capsule"])
    assert capsule.exists()
    text = capsule.read_text(encoding="utf-8")
    assert "--mode subagent" in text
    assert "subagent-done --role reviewer" in text


def test_subagent_done_updates_ledger_wait_and_inbox_path(
    orch: Orchestrator, tmp_path: Path
) -> None:
    ok, _ = orch.assign(
        "reviewer", str(tmp_path), "audit auth defaults", mode="subagent", project="subtest"
    )
    assert ok

    ok, message = orch.subagent_done("reviewer", "audit complete", project="subtest")

    assert ok is True
    assert "reported done" in message
    assert ("subtest", "reviewer") not in orch._subagent_assignments
    assert orch._wait_done_events[("subtest", "reviewer")]["failed"] is False
    notices = [call.args[1] for call in orch._notify_lead.call_args_list if len(call.args) > 1]
    assert any("done · subagent" in notice for notice in notices)
    ledger = task_ledger._load_state("subtest")
    assert ledger["groups"][0]["features"][0]["rows"][0]["status"] == "ok"


def test_subagent_mode_rejects_model_override(orch: Orchestrator, tmp_path: Path) -> None:
    ok, message = orch.assign(
        "reviewer",
        str(tmp_path),
        "audit",
        mode="subagent",
        model="different-model",
        project="subtest",
    )
    assert ok is False
    assert "model override" in message


def test_every_builtin_role_has_conditional_subagent_rule() -> None:
    role_files = sorted((REPO_ROOT / ".claude" / "agents").glob("*.md"))
    assert len(role_files) == 16
    for role_file in role_files:
        header = "\n".join(role_file.read_text(encoding="utf-8").splitlines()[:10])
        assert "--mode subagent" in header, role_file.name
        assert "ห้าม spawn subagent เอง" in header, role_file.name


def test_cross_provider_limitation_is_documented() -> None:
    patterns = (REPO_ROOT / "docs" / "lead" / "patterns.md").read_text(encoding="utf-8")
    assert "provider เดียวกับ parent เสมอ" in patterns
    assert "ไม่ใช่ model diversity" in patterns


def test_wait_tracks_pending_subagent_without_a_pane(orch: Orchestrator, tmp_path: Path) -> None:
    ok, _ = orch.assign(
        "reviewer", str(tmp_path), "audit auth defaults", mode="subagent", project="subtest"
    )
    assert ok

    registration = orch.begin_wait("subtest", ["reviewer"], 30)
    result = orch.poll_wait("subtest", registration["wait_id"])

    assert result["pending"] == {
        "reviewer": "native subagent ยังทำงานอยู่ (ไม่มี pane ตาม --mode subagent)"
    }
    assert result["gone"] == {}


def test_wait_without_roles_includes_pending_subagents(orch: Orchestrator, tmp_path: Path) -> None:
    ok, _ = orch.assign(
        "reviewer", str(tmp_path), "audit auth defaults", mode="subagent", project="subtest"
    )
    assert ok

    registration = orch.begin_wait("subtest", None, 30)

    assert registration["ok"] is True
    assert "reviewer" in registration["roles"]


def test_subagent_shards_use_consolidated_handoff(orch: Orchestrator, tmp_path: Path) -> None:
    orch._inject_shard_fanout_handoff = MagicMock()
    for shard in ("reviewer#1", "reviewer#2"):
        ok, _ = orch.assign(
            shard,
            str(tmp_path),
            "scan half the repository",
            mode="subagent",
            shard_total=2,
            project="subtest",
        )
        assert ok

    orch._notify_lead.reset_mock()
    assert orch.subagent_done("reviewer#1", "half one", project="subtest")[0]
    assert orch._notify_lead.call_count == 0
    assert orch._inject_shard_fanout_handoff.call_count == 0

    assert orch.subagent_done("reviewer#2", "half two", project="subtest")[0]
    assert orch._notify_lead.call_count == 0
    orch._inject_shard_fanout_handoff.assert_called_once()
