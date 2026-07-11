"""Structural smoke tests for the Phase 2 Skills + MCP Servers UI pages
(offscreen, no pytest-qt needed — mirrors test_settings_management_ui.py)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication

from agent_takkub import config, pane_tools_policy, shared_dev_tools, skill_policy
from agent_takkub.settings_management.commands import CreateMcpCommand, McpConfigDraft
from agent_takkub.settings_management.pages.mcp_page import McpPage
from agent_takkub.settings_management.pages.skills_page import SkillsPage
from agent_takkub.settings_management.repositories import mcps as mcps_repo
from agent_takkub.settings_management.window import SettingsManagementWindow


@pytest.fixture(autouse=True)
def redirect_stores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    shipped_dir = tmp_path / "shipped"
    shipped_dir.mkdir()
    monkeypatch.chdir(project_dir)
    monkeypatch.setattr(config, "REPO_ROOT", shipped_dir)
    monkeypatch.setattr(config, "ASSETS_ROOT", shipped_dir)
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "no-agents-here")
    monkeypatch.setattr(config, "CUSTOM_AGENTS_DIR", tmp_path / "no-custom-agents-here")
    monkeypatch.setattr(skill_policy, "SKILL_POLICY_FILE", tmp_path / "skill-policy.json")
    monkeypatch.setattr(shared_dev_tools, "SHARED_MCP_FILE", tmp_path / "shared-mcp.json")
    monkeypatch.setattr(pane_tools_policy, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")
    yield tmp_path


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_window_wires_skills_and_mcp_into_sidebar() -> None:
    window = SettingsManagementWindow()
    assert window.sidebar.item(1).text() == "Skills"
    assert window.sidebar.item(2).text() == "MCP Servers"
    window.sidebar.setCurrentRow(1)
    assert window.content_stack.currentWidget() is window.skills_page
    window.sidebar.setCurrentRow(2)
    assert window.content_stack.currentWidget() is window.mcp_page


def test_skills_page_create_flow_end_to_end() -> None:
    page = SkillsPage()
    page.refresh()
    page.on_new()
    page.name_edit.setText("my-skill")
    page.description_edit.setText("d")
    ok = page._save()
    assert ok is True
    names = {name for name, _ in page._load_rows()}
    assert "my-skill" in names


def test_skills_page_danger_zone_disabled_for_shipped_skill(tmp_path: Path) -> None:
    shipped_skill_dir = tmp_path / "shipped" / ".claude" / "skills" / "shipped-skill"
    shipped_skill_dir.mkdir(parents=True)
    (shipped_skill_dir / "SKILL.md").write_text(
        "---\nname: shipped-skill\ndescription: d\n---\n\nbody\n", encoding="utf-8"
    )
    page = SkillsPage()
    page.refresh()
    page.on_select("shipped-skill")
    assert page.danger_zone._delete_btn.isEnabled() is False
    assert page.duplicate_btn.isHidden() is False


def test_mcp_page_lists_managed_and_user_servers() -> None:
    shared_dev_tools.ensure_browser_mcps()
    mcps_repo.create(
        CreateMcpCommand(
            name="obsidian",
            config=McpConfigDraft(command="npx", args=["-y", "x"], env={}, type="stdio"),
        )
    )
    page = McpPage()
    page.refresh()
    names = {name for name, _ in page._load_rows()}
    assert "playwright" in names
    assert "obsidian" in names


def test_mcp_page_danger_zone_disabled_for_managed_server() -> None:
    shared_dev_tools.ensure_browser_mcps()
    page = McpPage()
    page.refresh()
    page.on_select("playwright")
    assert page.danger_zone._delete_btn.isEnabled() is False
    assert page.command_edit.isEnabled() is False


def test_mcp_page_manage_roles_button_switches_window_to_roles() -> None:
    window = SettingsManagementWindow()
    window.sidebar.setCurrentRow(2)
    window.mcp_page.manage_roles_requested()
    assert window.sidebar.currentRow() == 0
    assert window.content_stack.currentWidget() is window.roles_page
