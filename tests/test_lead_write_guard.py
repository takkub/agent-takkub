"""Tests for Lead vs teammate spawn argv permission flags.

Lead's write boundary is enforced by `pane_guard.evaluate_lead_direct_edit`
(a PreToolUse hook check on every Edit/Write call, see test_pane_guard.py
and test_team_preset_orchestrator.py) — not by anything baked into spawn's
argv. This file only covers the argv flags themselves:

  spawn(Lead)     → argv has --dangerously-skip-permissions, no --permission-mode
  spawn(teammate) → argv has --dangerously-skip-permissions too
"""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import config
from agent_takkub import orchestrator as orch_mod
from agent_takkub.orchestrator import Orchestrator

# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture
def two_project_json(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, seed_projects):
    """V2 project registry (#566) with two independent projects."""
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(orch_mod, "RUNTIME_DIR", runtime)
    cockpit = tmp_path / "cockpit"
    monkeypatch.setattr(config, "REPO_ROOT", cockpit)
    monkeypatch.setattr(orch_mod, "REPO_ROOT", cockpit)
    # lead_context.py has its own module-level RUNTIME_DIR/REPO_ROOT/
    # ASSETS_ROOT (used by _render_lead_context during spawn) — patch its
    # namespace too so spawn writes into tmp instead of the real runtime/.
    from agent_takkub import lead_context as lc_mod

    monkeypatch.setattr(lc_mod, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(lc_mod, "REPO_ROOT", cockpit)
    monkeypatch.setattr(lc_mod, "ASSETS_ROOT", cockpit)
    return seed_projects(
        tmp_path,
        {
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
            "empty_proj": {"paths": {}},
        },
        active="proj_a",
    )


# ─────────────────────────────────────────────────────────────
# spawn() argv: Lead vs teammate permission flags
# ─────────────────────────────────────────────────────────────


def _capture_spawn_argv(
    qapp: QCoreApplication,
    two_project_json: pathlib.Path,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    role_name: str,
    project: str = "proj_a",
) -> list[str]:
    """Helper: set up a minimal spawn() environment and return the argv
    that would be passed to PtySession.spawn (without actually spawning)."""
    # Cockpit CLAUDE.md is needed for _render_lead_context
    cockpit = tmp_path / "cockpit"
    cockpit.mkdir(parents=True, exist_ok=True)
    (cockpit / "CLAUDE.md").write_text("# Lead Guide\n", encoding="utf-8")
    monkeypatch.setattr(orch_mod, "REPO_ROOT", cockpit)
    monkeypatch.setattr(config, "REPO_ROOT", cockpit)

    captured: list[list[str]] = []

    def fake_pty_spawn(self_pty, argv, cwd, env, **kwargs):
        captured.append(list(argv))

    monkeypatch.setattr(orch_mod, "find_claude_executable", lambda: "claude")
    monkeypatch.setattr(orch_mod.PtySession, "spawn", fake_pty_spawn)
    # Suppress MCP config injection
    monkeypatch.setattr(orch_mod, "ensure_browser_mcps", lambda: (True, ""), raising=False)

    try:
        import agent_takkub.shared_dev_tools as sdt

        monkeypatch.setattr(sdt, "ensure_browser_mcps", lambda: (True, ""))
        monkeypatch.setattr(sdt, "shared_mcp_config_path", lambda: None)
        monkeypatch.setattr(sdt, "shared_mcp_config_path_for_role", lambda role: None)
    except Exception:
        pass

    # Patch shared_dev_tools import inside spawn
    fake_sdt = MagicMock()
    fake_sdt.ensure_browser_mcps.return_value = (True, "ok")
    fake_sdt.shared_mcp_config_path.return_value = None
    fake_sdt.shared_mcp_config_path_for_role.return_value = None
    monkeypatch.setattr(
        orch_mod,
        "ensure_browser_mcps",
        lambda: (True, "ok"),
        raising=False,
    )

    orch = Orchestrator()
    orch.shutdown_timers()

    # We need a fake pane in the right project slot
    fake_pane = MagicMock()
    fake_pane.session = None
    fake_pane.state = "empty"
    fake_pane.attach_session = MagicMock()
    orch._panes_by_project[project] = {role_name: fake_pane}

    # Patch PtySession processExited signal connect
    fake_session = MagicMock()
    fake_session.processExited = MagicMock()
    fake_session.processExited.connect = MagicMock()

    with patch.object(orch_mod.PtySession, "__new__", return_value=fake_session):
        with patch.object(
            fake_session,
            "spawn",
            side_effect=lambda argv, cwd, env, **kwargs: captured.append(list(argv)),
        ):
            # patch shared_dev_tools inside the spawn import
            with patch.dict(
                "sys.modules",
                {
                    "agent_takkub.shared_dev_tools": MagicMock(
                        ensure_browser_mcps=lambda: (True, "ok"),
                        shared_mcp_config_path=lambda: None,
                        shared_mcp_config_path_for_role=lambda role: None,
                    )
                },
            ):
                orch.spawn(role_name, project=project)

    return captured[0] if captured else []


class TestSpawnArgvLeadVsTeammate:
    def test_lead_argv_has_dangerously_skip_permissions(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Lead now runs with --dangerously-skip-permissions to eliminate
        per-tool permission prompts that disrupted flow."""
        argv = _capture_spawn_argv(qapp, two_project_json, tmp_path, monkeypatch, "lead")
        assert "--dangerously-skip-permissions" in argv

    def test_lead_argv_no_permission_mode_flag(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--permission-mode is redundant when --dangerously-skip-permissions
        is set — guard against re-introducing it by accident."""
        argv = _capture_spawn_argv(qapp, two_project_json, tmp_path, monkeypatch, "lead")
        assert "--permission-mode" not in argv

    def test_teammate_argv_has_dangerously_skip_permissions(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        argv = _capture_spawn_argv(qapp, two_project_json, tmp_path, monkeypatch, "backend")
        assert "--dangerously-skip-permissions" in argv
