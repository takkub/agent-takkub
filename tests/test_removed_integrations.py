"""Removed-integration boot cleanup (Figma) — runs on every install, backs up
first, touches only MCP/plugin containers, once per install."""

from __future__ import annotations

import json

import pytest

from agent_takkub import config, removed_integrations


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "data"
    claude = home / "claude-config"
    (claude / "plugins" / "cache" / "claude-plugins-official" / "figma" / "1.0").mkdir(parents=True)
    monkeypatch.setattr(config, "DATA_HOME", home)
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.setattr(config, "default_claude_config_dir", lambda: claude)

    def put(rel, data):
        p = home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data), encoding="utf-8")

    put(
        "claude-config/.claude.json",
        {
            "mcpServers": {"figma": {"url": "x"}, "ccc": {"cmd": "y"}},
            "projects": {"C:/work/figma-clone": {"mcpServers": {"figma": {}}, "keep": 1}},
            "pluginUsage": {"figma@claude-plugins-official": 3, "other@x": 1},
        },
    )
    put("claude-config/.credentials.json", {"mcpOAuth": {"figma|abc": {"t": 1}}, "claude": 1})
    put("claude-config/settings.json", {"enabledPlugins": {"figma@claude-plugins-official": True}})
    put("claude-config/mcp-needs-auth-cache.json", {"plugin:figma:figma": {}, "github": {}})
    put("capabilities/mcp/permissions.json", {"roles": {"frontend": {"mcps": ["figma", "ccc"]}}})
    put("runtime/shared-mcp-frontend.json", {"mcpServers": {"figma": {}}})
    put("providers/claude/default-x/settings.json", {"enabledPlugins": {"figma@o": True}})
    put("secrets/figma.json", {"token": "s"})
    (home / "skills" / "figma-use").mkdir(parents=True)
    return home


def _read(home, rel):
    return json.loads((home / rel).read_text(encoding="utf-8"))


def test_cleanup_removes_only_mcp_plugin_entries_and_backs_up_first(home):
    changed = removed_integrations.cleanup_integration("figma")
    assert changed == 10

    cj = _read(home, "claude-config/.claude.json")
    assert cj["mcpServers"] == {"ccc": {"cmd": "y"}}
    # a project PATH containing the word is the user's project — kept
    assert "C:/work/figma-clone" in cj["projects"]
    assert cj["projects"]["C:/work/figma-clone"] == {"mcpServers": {}, "keep": 1}
    assert cj["pluginUsage"] == {"other@x": 1}
    assert _read(home, "claude-config/.credentials.json") == {"mcpOAuth": {}, "claude": 1}
    assert _read(home, "claude-config/mcp-needs-auth-cache.json") == {"github": {}}
    assert _read(home, "capabilities/mcp/permissions.json")["roles"]["frontend"]["mcps"] == ["ccc"]
    assert not (home / "secrets" / "figma.json").exists()
    assert not (home / "skills" / "figma-use").exists()

    backups = list((home / "backups").glob("figma-removed-*"))
    assert len(backups) == 1
    b = backups[0]
    assert "figma" in _read(b, "claude-config/.claude.json")["mcpServers"]
    assert (b / "secrets" / "figma.json").is_file()
    assert (b / "skills" / "figma-use").is_dir()
    assert (b / "claude-config/plugins/cache/claude-plugins-official/figma").is_dir()

    assert removed_integrations.cleanup_integration("figma") == 0  # idempotent


def test_boot_cleanup_runs_once_per_install(home):
    assert removed_integrations.run_boot_cleanups() == {"figma": 10}
    # the user adds a figma MCP back on purpose — a later boot must not strip it
    p = home / "claude-config" / ".claude.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["mcpServers"]["figma"] = {"url": "mine"}
    p.write_text(json.dumps(data), encoding="utf-8")
    assert removed_integrations.run_boot_cleanups() == {}
    assert "figma" in _read(home, "claude-config/.claude.json")["mcpServers"]


def test_dev_checkout_is_never_touched(home, monkeypatch):
    monkeypatch.setattr(config, "REPO_ROOT", home)
    assert removed_integrations.run_boot_cleanups() == {}
    assert "figma" in _read(home, "claude-config/.claude.json")["mcpServers"]
