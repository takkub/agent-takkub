"""Tests for --auto-chain flag — one-hop impl→verify handoff.

Covers:
  assign(auto_chain=True) → state dict populated
  assign() default → state dict NOT populated
  done() → state cleared
  done() last auto-chain pane → injects handoff prompt to Lead
  done() with other auto-chain panes still pending → no handoff yet
  done() for non-auto-chain pane → no handoff
  close() → state cleared
  Multi-project isolation: proj_a auto-chain done does NOT trigger proj_b handoff
  Lead-absent handoff is queued via _pending_done_notices
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import config
from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def two_project_json(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """projects.json with two independent projects."""
    pj = tmp_path / "projects.json"
    pj.write_text(
        json.dumps(
            {
                "active": "proj_a",
                "projects": {
                    "proj_a": {
                        "paths": {
                            "api": str(tmp_path / "proj_a" / "api"),
                            "web": str(tmp_path / "proj_a" / "web"),
                        }
                    },
                    "proj_b": {
                        "paths": {
                            "api": str(tmp_path / "proj_b" / "api"),
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "PROJECTS_JSON", pj)
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", runtime)
    cockpit = tmp_path / "cockpit"
    monkeypatch.setattr(config, "REPO_ROOT", cockpit)
    monkeypatch.setattr(orch_mod, "REPO_ROOT", cockpit)
    return pj


def _make_orch_with_fake_panes(
    project: str,
    roles_with_session: list[str],
) -> tuple[Orchestrator, dict[str, MagicMock]]:
    """Build an Orchestrator and pre-populate fake panes for the given roles.
    Returns (orch, {role: pane}) so tests can inspect pane.session.write calls."""
    orch = Orchestrator()
    orch._idle_watchdog.stop()
    panes: dict[str, MagicMock] = {}
    for role in roles_with_session:
        pane = MagicMock()
        pane._session_cwd = "/tmp"
        pane._transcript_path = None
        pane.session = MagicMock()
        pane.session.is_alive = True
        pane.session.write = MagicMock()
        pane.set_state = MagicMock()
        pane.mark_expected_exit = MagicMock()
        panes[role] = pane
    orch._panes_by_project[project] = panes
    return orch, panes


class TestAutoChainStateLifecycle:
    def test_assign_with_auto_chain_populates_state(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch, _ = _make_orch_with_fake_panes("proj_a", ["lead", "frontend"])
        monkeypatch.setattr(orch, "spawn", MagicMock(return_value=(True, "ok")))
        monkeypatch.setattr(orch, "_send_when_ready", MagicMock())
        orch.assign("frontend", cwd="/tmp", task="ui", auto_chain=True, project="proj_a")
        assert orch._auto_chain_panes.get("proj_a::frontend") is True

    def test_assign_without_auto_chain_does_not_populate_state(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch, _ = _make_orch_with_fake_panes("proj_a", ["lead", "frontend"])
        monkeypatch.setattr(orch, "spawn", MagicMock(return_value=(True, "ok")))
        monkeypatch.setattr(orch, "_send_when_ready", MagicMock())
        orch.assign("frontend", cwd="/tmp", task="ui", project="proj_a")
        assert "proj_a::frontend" not in orch._auto_chain_panes

    def test_close_clears_auto_chain_state(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch, _ = _make_orch_with_fake_panes("proj_a", ["lead", "frontend"])
        orch._auto_chain_panes["proj_a::frontend"] = True
        orch.close("frontend", project="proj_a", force=True)
        assert "proj_a::frontend" not in orch._auto_chain_panes


class TestInjectAutoChainHandoff:
    def test_writes_handoff_prompt_to_alive_lead(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch, panes = _make_orch_with_fake_panes("proj_a", ["lead", "frontend"])
        orch._inject_auto_chain_handoff("proj_a")
        lead_writes = [c.args[0] for c in panes["lead"].session.write.call_args_list]
        assert any("auto-chain handoff" in str(w) for w in lead_writes)

    def test_writes_queue_when_lead_absent(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch, _ = _make_orch_with_fake_panes("proj_a", ["frontend"])  # no lead
        orch._inject_auto_chain_handoff("proj_a")
        queue = orch._pending_done_notices.get("proj_a", [])
        assert any("auto-chain handoff" in entry.get("body", "") for entry in queue)


class TestDoneAutoChainTrigger:
    def test_done_last_auto_chain_pane_fires_handoff(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Single auto-chain pane: done() fires handoff immediately."""
        orch, panes = _make_orch_with_fake_panes("proj_a", ["lead", "frontend"])
        orch._auto_chain_panes["proj_a::frontend"] = True
        monkeypatch.setattr(orch, "close", MagicMock(return_value=(True, "ok")))
        monkeypatch.setattr(orch, "_save_decision_note", MagicMock())

        orch.done("frontend", note="UI shipped", project="proj_a")

        lead_writes = [c.args[0] for c in panes["lead"].session.write.call_args_list]
        assert any("[frontend done]" in str(w) for w in lead_writes)
        assert any("auto-chain handoff" in str(w) for w in lead_writes)

    def test_done_not_last_auto_chain_pane_no_handoff(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two auto-chain panes; first done → no handoff yet."""
        orch, panes = _make_orch_with_fake_panes("proj_a", ["lead", "frontend", "backend"])
        orch._auto_chain_panes["proj_a::frontend"] = True
        orch._auto_chain_panes["proj_a::backend"] = True
        monkeypatch.setattr(orch, "close", MagicMock(return_value=(True, "ok")))
        monkeypatch.setattr(orch, "_save_decision_note", MagicMock())

        orch.done("frontend", note="UI shipped", project="proj_a")

        lead_writes = [c.args[0] for c in panes["lead"].session.write.call_args_list]
        assert any("[frontend done]" in str(w) for w in lead_writes)
        assert not any("auto-chain handoff" in str(w) for w in lead_writes)

    def test_done_non_auto_chain_pane_no_handoff(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pane that was assigned without --auto-chain → no handoff."""
        orch, panes = _make_orch_with_fake_panes("proj_a", ["lead", "frontend"])
        # NOTE: _auto_chain_panes deliberately NOT set
        monkeypatch.setattr(orch, "close", MagicMock(return_value=(True, "ok")))
        monkeypatch.setattr(orch, "_save_decision_note", MagicMock())

        orch.done("frontend", note="just a scout", project="proj_a")

        lead_writes = [c.args[0] for c in panes["lead"].session.write.call_args_list]
        assert any("[frontend done]" in str(w) for w in lead_writes)
        assert not any("auto-chain handoff" in str(w) for w in lead_writes)

    def test_done_clears_auto_chain_key_after_handoff(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orch, _ = _make_orch_with_fake_panes("proj_a", ["lead", "frontend"])
        orch._auto_chain_panes["proj_a::frontend"] = True
        monkeypatch.setattr(orch, "close", MagicMock(return_value=(True, "ok")))
        monkeypatch.setattr(orch, "_save_decision_note", MagicMock())

        orch.done("frontend", note="UI shipped", project="proj_a")

        assert "proj_a::frontend" not in orch._auto_chain_panes

    def test_multi_project_isolation(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Auto-chain done in proj_a does NOT trigger handoff for proj_b."""
        orch, panes_a = _make_orch_with_fake_panes("proj_a", ["lead", "frontend"])
        lead_b = MagicMock()
        lead_b.session = MagicMock()
        lead_b.session.is_alive = True
        lead_b.session.write = MagicMock()
        orch._panes_by_project["proj_b"] = {"lead": lead_b}

        orch._auto_chain_panes["proj_a::frontend"] = True
        orch._auto_chain_panes["proj_b::backend"] = True  # still pending in proj_b
        monkeypatch.setattr(orch, "close", MagicMock(return_value=(True, "ok")))
        monkeypatch.setattr(orch, "_save_decision_note", MagicMock())

        orch.done("frontend", note="proj_a UI", project="proj_a")

        a_writes = [c.args[0] for c in panes_a["lead"].session.write.call_args_list]
        assert any("auto-chain handoff" in str(w) for w in a_writes)
        b_writes = [c.args[0] for c in lead_b.session.write.call_args_list]
        assert not any("auto-chain handoff" in str(w) for w in b_writes)
        assert "proj_b::backend" in orch._auto_chain_panes
