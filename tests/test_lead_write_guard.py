"""Tests for Phase 2a: Lead write-boundary enforcement via permissions.deny.

Covers:
  render_lead_settings(project) → generates deny rules for project paths
  spawn(Lead)  → argv has --settings <guard-path>, NO --dangerously-skip-permissions
  spawn(teammate) → argv still has --dangerously-skip-permissions, NO --settings guard
  Multi-project isolation: each Lead gets deny rules for its own project only
  Idempotency: calling render_lead_settings twice returns same path, same content
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from agent_takkub import config, orchestrator as orch_mod
from agent_takkub.orchestrator import (
    _LEAD_GUARD_WRITE_TOOLS,
    Orchestrator,
    render_lead_settings,
)


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
                    "empty_proj": {
                        "paths": {}
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


# ─────────────────────────────────────────────────────────────
# render_lead_settings: output file location and content
# ─────────────────────────────────────────────────────────────


class TestRenderLeadSettings:
    def test_creates_file_in_runtime_dir(
        self, two_project_json: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        result = render_lead_settings("proj_a")
        assert result.parent == tmp_path / "runtime"
        assert result.name == "lead-guard-proj_a.json"
        assert result.exists()

    def test_output_is_valid_json(
        self, two_project_json: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        result = render_lead_settings("proj_a")
        data = json.loads(result.read_text(encoding="utf-8"))
        assert isinstance(data, dict)

    def test_has_permissions_deny_key(
        self, two_project_json: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        result = render_lead_settings("proj_a")
        data = json.loads(result.read_text(encoding="utf-8"))
        assert "permissions" in data
        assert "deny" in data["permissions"]
        assert isinstance(data["permissions"]["deny"], list)

    def test_deny_rules_contain_all_write_tools_for_each_path(
        self, two_project_json: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        result = render_lead_settings("proj_a")
        data = json.loads(result.read_text(encoding="utf-8"))
        deny = data["permissions"]["deny"]

        api_path = (tmp_path / "proj_a" / "api").resolve().as_posix()
        web_path = (tmp_path / "proj_a" / "web").resolve().as_posix()

        for tool in _LEAD_GUARD_WRITE_TOOLS:
            assert f"{tool}({api_path}/**)" in deny, (
                f"{tool} deny rule for api path missing"
            )
            assert f"{tool}({web_path}/**)" in deny, (
                f"{tool} deny rule for web path missing"
            )

    def test_deny_rules_do_not_contain_other_project_paths(
        self, two_project_json: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        result = render_lead_settings("proj_a")
        data = json.loads(result.read_text(encoding="utf-8"))
        deny = data["permissions"]["deny"]
        deny_str = json.dumps(deny)

        proj_b_api = (tmp_path / "proj_b" / "api").resolve().as_posix()
        assert proj_b_api not in deny_str, (
            "proj_b path must NOT appear in proj_a Lead guard"
        )

    def test_has_default_mode_accept_edits(
        self, two_project_json: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        result = render_lead_settings("proj_a")
        data = json.loads(result.read_text(encoding="utf-8"))
        assert data["permissions"].get("defaultMode") == "acceptEdits"

    def test_empty_project_gives_empty_deny_list(
        self, two_project_json: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        result = render_lead_settings("empty_proj")
        data = json.loads(result.read_text(encoding="utf-8"))
        assert data["permissions"]["deny"] == []

    def test_unknown_project_gives_empty_deny_list(
        self, two_project_json: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        result = render_lead_settings("nonexistent")
        data = json.loads(result.read_text(encoding="utf-8"))
        assert data["permissions"]["deny"] == []


# ─────────────────────────────────────────────────────────────
# render_lead_settings: idempotency
# ─────────────────────────────────────────────────────────────


class TestRenderLeadSettingsIdempotency:
    def test_same_path_returned_on_second_call(
        self, two_project_json: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        first = render_lead_settings("proj_a")
        second = render_lead_settings("proj_a")
        assert first == second

    def test_same_content_on_second_call(
        self, two_project_json: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        first = render_lead_settings("proj_a")
        content_first = first.read_text(encoding="utf-8")
        second = render_lead_settings("proj_a")
        content_second = second.read_text(encoding="utf-8")
        assert content_first == content_second

    def test_file_reflects_path_changes_on_regenerate(
        self, two_project_json: pathlib.Path, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If projects.json paths change, render_lead_settings picks them up."""
        render_lead_settings("proj_a")

        # Modify projects.json with a new path
        new_pj = tmp_path / "projects.json"
        new_data = json.loads(new_pj.read_text(encoding="utf-8"))
        new_data["projects"]["proj_a"]["paths"]["extra"] = str(tmp_path / "proj_a" / "extra")
        new_pj.write_text(json.dumps(new_data), encoding="utf-8")

        result = render_lead_settings("proj_a")
        data = json.loads(result.read_text(encoding="utf-8"))
        deny_str = json.dumps(data["permissions"]["deny"])
        extra_path = (tmp_path / "proj_a" / "extra").resolve().as_posix()
        assert extra_path in deny_str, "New path must appear after regeneration"


# ─────────────────────────────────────────────────────────────
# Multi-project isolation
# ─────────────────────────────────────────────────────────────


class TestMultiProjectIsolation:
    def test_separate_files_for_separate_projects(
        self, two_project_json: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        path_a = render_lead_settings("proj_a")
        path_b = render_lead_settings("proj_b")
        assert path_a != path_b
        assert path_a.name == "lead-guard-proj_a.json"
        assert path_b.name == "lead-guard-proj_b.json"

    def test_proj_a_guard_denies_only_proj_a_paths(
        self, two_project_json: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        path_a = render_lead_settings("proj_a")
        data_a = json.loads(path_a.read_text(encoding="utf-8"))
        deny_str = json.dumps(data_a["permissions"]["deny"])

        proj_a_api = (tmp_path / "proj_a" / "api").resolve().as_posix()
        proj_b_api = (tmp_path / "proj_b" / "api").resolve().as_posix()

        assert proj_a_api in deny_str
        assert proj_b_api not in deny_str

    def test_proj_b_guard_denies_only_proj_b_paths(
        self, two_project_json: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        path_b = render_lead_settings("proj_b")
        data_b = json.loads(path_b.read_text(encoding="utf-8"))
        deny_str = json.dumps(data_b["permissions"]["deny"])

        proj_a_api = (tmp_path / "proj_a" / "api").resolve().as_posix()
        proj_b_api = (tmp_path / "proj_b" / "api").resolve().as_posix()

        assert proj_b_api in deny_str
        assert proj_a_api not in deny_str


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

    def fake_pty_spawn(self_pty, argv, cwd, env):  # noqa: ANN001
        captured.append(list(argv))

    monkeypatch.setattr(orch_mod, "find_claude_executable", lambda: "claude")
    monkeypatch.setattr(orch_mod.PtySession, "spawn", fake_pty_spawn)
    # Suppress MCP config injection
    monkeypatch.setattr(orch_mod, "ensure_browser_mcps", lambda: (True, ""), raising=False)

    try:
        import agent_takkub.shared_dev_tools as sdt  # noqa: PLC0415

        monkeypatch.setattr(sdt, "ensure_browser_mcps", lambda: (True, ""))
        monkeypatch.setattr(sdt, "shared_mcp_config_path", lambda: None)
    except Exception:
        pass

    # Patch shared_dev_tools import inside spawn
    fake_sdt = MagicMock()
    fake_sdt.ensure_browser_mcps.return_value = (True, "ok")
    fake_sdt.shared_mcp_config_path.return_value = None
    monkeypatch.setattr(
        orch_mod,
        "ensure_browser_mcps",
        lambda: (True, "ok"),
        raising=False,
    )

    orch = Orchestrator()
    orch._idle_watchdog.stop()

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
        with patch.object(fake_session, "spawn", side_effect=lambda argv, cwd, env: captured.append(list(argv))):
            # patch shared_dev_tools inside the spawn import
            with patch.dict(
                "sys.modules",
                {
                    "agent_takkub.shared_dev_tools": MagicMock(
                        ensure_browser_mcps=lambda: (True, "ok"),
                        shared_mcp_config_path=lambda: None,
                    )
                },
            ):
                orch.spawn(role_name, project=project)

    return captured[0] if captured else []


class TestSpawnArgvLeadVsTeammate:
    def test_lead_argv_has_permission_mode_accept_edits(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        argv = _capture_spawn_argv(qapp, two_project_json, tmp_path, monkeypatch, "lead")
        assert "--permission-mode" in argv
        idx = argv.index("--permission-mode")
        assert argv[idx + 1] == "acceptEdits"

    def test_lead_argv_no_dangerously_skip_permissions(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        argv = _capture_spawn_argv(qapp, two_project_json, tmp_path, monkeypatch, "lead")
        assert "--dangerously-skip-permissions" not in argv

    def test_lead_argv_has_settings_guard_path(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        argv = _capture_spawn_argv(qapp, two_project_json, tmp_path, monkeypatch, "lead")
        assert "--settings" in argv
        idx = argv.index("--settings")
        settings_path = pathlib.Path(argv[idx + 1])
        assert settings_path.name == "lead-guard-proj_a.json"

    def test_teammate_argv_has_dangerously_skip_permissions(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        argv = _capture_spawn_argv(qapp, two_project_json, tmp_path, monkeypatch, "backend")
        assert "--dangerously-skip-permissions" in argv

    def test_teammate_argv_no_lead_guard_settings(
        self,
        qapp: QCoreApplication,
        two_project_json: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        argv = _capture_spawn_argv(qapp, two_project_json, tmp_path, monkeypatch, "backend")
        # No --settings pointing to lead-guard file
        settings_values = [
            argv[i + 1] for i, v in enumerate(argv) if v == "--settings"
        ]
        for sv in settings_values:
            assert "lead-guard" not in sv, (
                "teammate must not receive lead write-guard settings"
            )
