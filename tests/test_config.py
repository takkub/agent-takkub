"""Tests for config helpers (project / runtime / role-aware cwd)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_takkub import config


@pytest.fixture
def projects_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect config.* paths to a tmp dir with a minimal projects.json."""
    pj = tmp_path / "projects.json"
    pj.write_text(
        json.dumps(
            {
                "active": "demo",
                "projects": {
                    "demo": {
                        "paths": {
                            "web": "/tmp/demo/web",
                            "api": "/tmp/demo/api",
                            "mobile": "/tmp/demo/mobile",
                        },
                        "presets": ["frontend", "backend"],
                    },
                    "empty": {"paths": {}},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "PROJECTS_JSON", pj)
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / ".claude" / "agents")
    monkeypatch.setattr(config, "EVENTS_LOG", tmp_path / "runtime" / "events.log")
    monkeypatch.setattr(config, "PORT_FILE", tmp_path / "runtime" / "port")
    return pj


class TestActiveProject:
    def test_returns_name_and_dict(self, projects_file: Path) -> None:
        name, proj = config.active_project()
        assert name == "demo"
        assert proj["paths"]["web"] == "/tmp/demo/web"

    def test_no_active_returns_none(
        self, projects_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        projects_file.write_text(json.dumps({"projects": {}}), encoding="utf-8")
        name, proj = config.active_project()
        assert name is None
        assert proj == {}

    def test_missing_projects_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "PROJECTS_JSON", tmp_path / "nope.json")
        name, proj = config.active_project()
        assert name is None
        assert proj == {}


class TestListProjectNames:
    def test_lists_all(self, projects_file: Path) -> None:
        assert set(config.list_project_names()) == {"demo", "empty"}


class TestSetActiveProject:
    def test_valid_name_persists(self, projects_file: Path) -> None:
        assert config.set_active_project("empty") is True
        data = json.loads(projects_file.read_text(encoding="utf-8"))
        assert data["active"] == "empty"

    def test_invalid_name_returns_false(self, projects_file: Path) -> None:
        assert config.set_active_project("nope") is False
        data = json.loads(projects_file.read_text(encoding="utf-8"))
        assert data["active"] == "demo"  # unchanged


class TestDefaultCwdForRole:
    def test_frontend_picks_web(self, projects_file: Path) -> None:
        assert config.default_cwd_for_role("frontend") == "/tmp/demo/web"

    def test_backend_picks_api(self, projects_file: Path) -> None:
        assert config.default_cwd_for_role("backend") == "/tmp/demo/api"

    def test_mobile_picks_mobile_when_available(self, projects_file: Path) -> None:
        assert config.default_cwd_for_role("mobile") == "/tmp/demo/mobile"

    def test_unknown_role_falls_back_to_first_path(self, projects_file: Path) -> None:
        # ordering: web comes first in the demo project paths
        assert config.default_cwd_for_role("data-eng") == "/tmp/demo/web"

    def test_empty_project_returns_none(
        self, projects_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # flip active to the "empty" project (no paths)
        config.set_active_project("empty")
        assert config.default_cwd_for_role("frontend") is None


class TestPresetRoles:
    def test_returns_configured_presets(self, projects_file: Path) -> None:
        assert config.preset_roles_for_active() == ["frontend", "backend"]

    def test_lowercases_and_trims(
        self, projects_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data = json.loads(projects_file.read_text(encoding="utf-8"))
        data["projects"]["demo"]["presets"] = ["  FRONTEND ", "Backend", ""]
        projects_file.write_text(json.dumps(data), encoding="utf-8")
        assert config.preset_roles_for_active() == ["frontend", "backend"]

    def test_no_presets_returns_empty(
        self, projects_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config.set_active_project("empty")
        assert config.preset_roles_for_active() == []


class TestPortFile:
    def test_roundtrip(self, projects_file: Path) -> None:
        assert config.read_port() is None
        config.write_port(54321)
        assert config.read_port() == 54321

    def test_corrupt_port_file_returns_none(self, projects_file: Path) -> None:
        config.ensure_runtime()
        config.PORT_FILE.write_text("not-a-number", encoding="utf-8")
        assert config.read_port() is None


class TestAgentRoleDir:
    """Verify that agent_role_dir() materialises CLAUDE.md with the central
    hygiene blocks appended (dev-server + non-interactive shell — issue #52).
    """

    def test_creates_dir_when_no_agent_file(self, projects_file: Path) -> None:
        d = config.agent_role_dir("frontend")
        assert d.is_dir()

    def test_no_claude_md_when_no_source_file(self, projects_file: Path) -> None:
        # When .claude/agents/frontend.md doesn't exist, no CLAUDE.md is written.
        d = config.agent_role_dir("frontend")
        assert not (d / "CLAUDE.md").exists()

    def test_dev_server_hygiene_appended(self, projects_file: Path) -> None:
        # Write a minimal source agent file.
        agents_dir = projects_file.parent / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "backend.md").write_text("# Backend role\nDo stuff.\n", encoding="utf-8")
        monkeypatch_config_agents_dir = config.AGENTS_DIR
        config.AGENTS_DIR = agents_dir
        try:
            d = config.agent_role_dir("backend")
            content = (d / "CLAUDE.md").read_text(encoding="utf-8")
            assert "next build && next start" in content
            assert "รัน web/dev server" in content
        finally:
            config.AGENTS_DIR = monkeypatch_config_agents_dir

    def test_non_interactive_hygiene_appended(self, projects_file: Path) -> None:
        # Issue #52 Layer 4: the non-interactive shell rules must be present.
        agents_dir = projects_file.parent / ".claude" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "frontend.md").write_text("# Frontend role\n", encoding="utf-8")
        old_agents_dir = config.AGENTS_DIR
        config.AGENTS_DIR = agents_dir
        try:
            d = config.agent_role_dir("frontend")
            content = (d / "CLAUDE.md").read_text(encoding="utf-8")
            assert "npx --yes" in content
            assert "GIT_TERMINAL_PROMPT" in content
            assert "non-interactive" in content
        finally:
            config.AGENTS_DIR = old_agents_dir

    def test_frontmatter_stripped(self, projects_file: Path) -> None:
        agents_dir = projects_file.parent / ".claude" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        source = "---\ntitle: QA\n---\n# QA role body\n"
        (agents_dir / "qa.md").write_text(source, encoding="utf-8")
        old_agents_dir = config.AGENTS_DIR
        config.AGENTS_DIR = agents_dir
        try:
            d = config.agent_role_dir("qa")
            content = (d / "CLAUDE.md").read_text(encoding="utf-8")
            assert "---" not in content
            assert "QA role body" in content
        finally:
            config.AGENTS_DIR = old_agents_dir


class TestAssetsRootAndCliBinDir:
    """installed prod builds resolve REPO_ROOT into an empty venv/Lib
    ancestor (no CLAUDE.md, no .claude/agents/, no bin/) — ASSETS_ROOT and
    CLI_BIN_DIR must route around that instead of assuming REPO_ROOT is the
    real repo checkout."""

    def test_agents_dir_derives_from_assets_root(self) -> None:
        # Locks the relationship so a future edit can't silently point
        # AGENTS_DIR back at REPO_ROOT.
        assert config.AGENTS_DIR == config.ASSETS_ROOT / ".claude" / "agents"

    def test_dev_checkout_assets_root_is_repo_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(config, "DATA_HOME", tmp_path)
        assert config._resolve_assets_root() == tmp_path

    def test_dev_checkout_cli_bin_dir_is_repo_root_bin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(config, "DATA_HOME", tmp_path)
        assert config._resolve_cli_bin_dir() == tmp_path / "bin"

    def test_installed_assets_root_is_package_assets_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate an installed layout: REPO_ROOT resolves into a venv
        # ancestor that isn't DATA_HOME (the isolated ~/.agent-takkub).
        venv_lib = tmp_path / "venv" / "Lib"
        monkeypatch.setattr(config, "REPO_ROOT", venv_lib)
        monkeypatch.setattr(config, "DATA_HOME", tmp_path / "agent-takkub-home")
        expected = Path(config.__file__).resolve().parent / "_assets"
        assert config._resolve_assets_root() == expected

    def test_installed_cli_bin_dir_derives_from_sys_executable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        venv_lib = tmp_path / "venv" / "Lib"
        scripts_dir = tmp_path / "venv" / "Scripts"
        monkeypatch.setattr(config, "REPO_ROOT", venv_lib)
        monkeypatch.setattr(config, "DATA_HOME", tmp_path / "agent-takkub-home")
        monkeypatch.setattr(sys, "executable", str(scripts_dir / "pythonw.exe"))
        assert config._resolve_cli_bin_dir() == scripts_dir


class TestDefaultClaudeConfigDir:
    """Per-instance default Claude profile home (isolation plan, finding C5)."""

    def test_dev_checkout_is_dot_claude_under_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(config, "DATA_HOME", tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        assert config.default_claude_config_dir() == tmp_path / "home" / ".claude"

    def test_installed_is_isolated_under_data_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "venv-lib")
        monkeypatch.setattr(config, "DATA_HOME", tmp_path / "agent-takkub-home")
        assert (
            config.default_claude_config_dir() == tmp_path / "agent-takkub-home" / "claude-config"
        )


class TestInstanceIdentityLabel:
    """Window-title / app-display-name identity tag (isolation plan, finding C2)."""

    def test_dev_checkout_label(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = tmp_path / "agent-takkub"
        repo.mkdir()
        monkeypatch.setattr(config, "REPO_ROOT", repo)
        monkeypatch.setattr(config, "DATA_HOME", repo)
        assert config.instance_identity_label() == f"dev · {repo.name}"

    def test_installed_label_includes_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "venv-lib")
        monkeypatch.setattr(config, "DATA_HOME", tmp_path / "agent-takkub-home")
        monkeypatch.setattr(config, "instance_display_version", lambda: "1.2.3")
        assert config.instance_identity_label() == "v1.2.3"

    def test_dev_version_reads_pyproject(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(config, "DATA_HOME", tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "agent-takkub"\nversion = "9.9.9"\n', encoding="utf-8"
        )
        assert config.instance_display_version() == "9.9.9"

    def test_installed_version_uses_importlib_metadata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "venv-lib")
        monkeypatch.setattr(config, "DATA_HOME", tmp_path / "agent-takkub-home")
        with patch("importlib.metadata.version", return_value="2.0.0"):
            assert config.instance_display_version() == "2.0.0"


class TestInstanceWindowTitle:
    """Full window-title identity segment — dev keeps a bracketed tag,
    installed builds show the bare version (no literal "prod" word)."""

    def test_dev_checkout_is_bracketed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "agent-takkub"
        repo.mkdir()
        monkeypatch.setattr(config, "REPO_ROOT", repo)
        monkeypatch.setattr(config, "DATA_HOME", repo)
        assert config.instance_window_title() == f"agent-takkub [dev · {repo.name}]"

    def test_installed_is_bare_version_no_prod_word(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "venv-lib")
        monkeypatch.setattr(config, "DATA_HOME", tmp_path / "agent-takkub-home")
        monkeypatch.setattr(config, "instance_display_version", lambda: "1.0.14")
        title = config.instance_window_title()
        assert title == "agent-takkub v1.0.14"
        assert "prod" not in title
