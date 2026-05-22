"""Tests for `Orchestrator.broadcast_bug_check` — the "เจอบัค" UI button.

When user clicks the bug-check button in cockpit, every active pane in the
current project gets a prompt asking the agent inside to introspect the
session for cockpit-level bugs and either `takkub issue new` (if found) or
`takkub send --to lead 'no bugs'` (if clean).

Scope rules:
  * Only panes in the chosen project are prompted (no cross-project bleed).
  * Empty / dead-session slots are skipped silently.
  * The prompt text must mention `takkub issue new` so agents follow the
    documented workflow rather than freelancing.
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

ACTIVE_PROJECT = "proj"


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def minimal_project_json(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    pj = tmp_path / "projects.json"
    pj.write_text(
        json.dumps(
            {
                "active": ACTIVE_PROJECT,
                "projects": {
                    ACTIVE_PROJECT: {
                        "paths": {
                            "api": str(tmp_path / "proj" / "api"),
                            "web": str(tmp_path / "proj" / "web"),
                        }
                    },
                    "other": {"paths": {"api": str(tmp_path / "other" / "api")}},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "PROJECTS_JSON", pj)
    cockpit = tmp_path / "cockpit"
    monkeypatch.setattr(config, "REPO_ROOT", cockpit)
    monkeypatch.setattr(orch_mod, "REPO_ROOT", cockpit)
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", runtime)
    return pj


@pytest.fixture
def orch(
    qapp: QCoreApplication,
    minimal_project_json: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Orchestrator:
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_project",
        staticmethod(lambda project: project or ACTIVE_PROJECT),
    )
    o = Orchestrator()
    o._idle_watchdog.stop()
    # Replace QTimer.singleShot side-effects so prompts dispatch synchronously.
    monkeypatch.setattr(orch_mod.QTimer, "singleShot", staticmethod(lambda *_: None))
    return o


def _live_pane() -> MagicMock:
    """Mock AgentPane with a session that reports alive."""
    pane = MagicMock()
    pane.session = MagicMock()
    pane.session.is_alive = True
    pane.session.is_at_ready_prompt.return_value = True
    return pane


def _dead_pane() -> MagicMock:
    """Mock AgentPane with no session attached."""
    pane = MagicMock()
    pane.session = None
    return pane


class TestBroadcastBugCheck:
    def test_no_panes_returns_zero(self, orch: Orchestrator) -> None:
        """Empty project → (0, [])."""
        orch._panes_by_project.clear()
        count, roles = orch.broadcast_bug_check(project=ACTIVE_PROJECT)
        assert count == 0
        assert roles == []

    def test_prompts_each_live_pane(self, orch: Orchestrator) -> None:
        """Lead + backend + frontend alive → all three get a prompt."""
        orch._panes_by_project[ACTIVE_PROJECT] = {
            "lead": _live_pane(),
            "backend": _live_pane(),
            "frontend": _live_pane(),
        }
        # Spy on _send_when_ready so we can inspect what each pane received.
        sent: list[tuple[str, str]] = []
        orch._send_when_ready = lambda role, task, **_kw: sent.append((role, task))  # type: ignore[assignment]

        count, roles = orch.broadcast_bug_check(project=ACTIVE_PROJECT)
        assert count == 3
        assert set(roles) == {"lead", "backend", "frontend"}
        assert len(sent) == 3
        assert {role for role, _ in sent} == {"lead", "backend", "frontend"}

    def test_skips_dead_sessions(self, orch: Orchestrator) -> None:
        """Pane with session=None must not be prompted."""
        orch._panes_by_project[ACTIVE_PROJECT] = {
            "lead": _live_pane(),
            "backend": _dead_pane(),  # session is None → skip
            "frontend": _live_pane(),
        }
        sent: list[str] = []
        orch._send_when_ready = lambda role, task, **_kw: sent.append(role)  # type: ignore[assignment]

        count, roles = orch.broadcast_bug_check(project=ACTIVE_PROJECT)
        assert count == 2
        assert "backend" not in roles
        assert "backend" not in sent

    def test_prompt_contains_issue_new_directive(self, orch: Orchestrator) -> None:
        """Generated prompt must instruct agents to use `takkub issue new`."""
        orch._panes_by_project[ACTIVE_PROJECT] = {"backend": _live_pane()}
        captured: list[str] = []
        orch._send_when_ready = lambda role, task, **_kw: captured.append(task)  # type: ignore[assignment]

        orch.broadcast_bug_check(project=ACTIVE_PROJECT)
        assert len(captured) == 1
        prompt = captured[0]
        assert "takkub issue new" in prompt
        assert ACTIVE_PROJECT in prompt  # --noticed-in <project>
        assert "backend" in prompt  # --role <role>

    def test_prompt_offers_no_bug_path(self, orch: Orchestrator) -> None:
        """Prompt must give agents a 'no bugs' escape so they don't invent issues."""
        orch._panes_by_project[ACTIVE_PROJECT] = {"backend": _live_pane()}
        captured: list[str] = []
        orch._send_when_ready = lambda role, task, **_kw: captured.append(task)  # type: ignore[assignment]

        orch.broadcast_bug_check(project=ACTIVE_PROJECT)
        prompt = captured[0]
        # Some "no bugs to report" signal must be present so claude doesn't
        # feel forced to fabricate an issue when the session was clean.
        assert "no bug" in prompt.lower() or "ไม่มีบัค" in prompt or "ไม่เจอ" in prompt

    def test_project_scoped(self, orch: Orchestrator) -> None:
        """Panes under 'other' project must not be prompted when broadcast targets 'proj'."""
        orch._panes_by_project[ACTIVE_PROJECT] = {"backend": _live_pane()}
        orch._panes_by_project["other"] = {"backend": _live_pane()}
        sent: list[tuple[str, str]] = []
        # Capture project kwarg too so we can confirm scoping.
        orch._send_when_ready = (  # type: ignore[assignment]
            lambda role, task, project=None, **_kw: sent.append((role, project or ""))
        )

        count, _ = orch.broadcast_bug_check(project=ACTIVE_PROJECT)
        assert count == 1
        # Only the proj-scoped backend received a prompt.
        assert all(p == ACTIVE_PROJECT for _, p in sent)
