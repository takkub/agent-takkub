"""Tests for MCP role-split: render_lead_mcp_config() + render_teammate_mcp_config().

Lead gets all pms tools (read + write) + browser MCPs.
Teammates get read-only pms tools + browser MCPs (no create/update/add_comment).
"""

from __future__ import annotations

import json
import pathlib

import pytest

from agent_takkub.shared_dev_tools import (
    BROWSER_MCPS,
    render_lead_mcp_config,
    render_teammate_mcp_config,
    write_shared_mcp_config,
)

_WRITE_TOOLS = {
    "mcp__pms__pms_create_task",
    "mcp__pms__pms_update_task",
    "mcp__pms__pms_add_comment",
}
_READ_TOOLS = {
    "mcp__pms__pms_preview_task",
    "mcp__pms__pms_get_task",
    "mcp__pms__pms_list_tasks",
    "mcp__pms__pms_list_workspaces",
    "mcp__pms__pms_list_spaces",
    "mcp__pms__pms_list_lists",
    "mcp__pms__pms_list_statuses",
    "mcp__pms__pms_resolve_list",
}


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    """Point RUNTIME_DIR to tmp_path and write a valid shared-mcp.json."""
    import agent_takkub.config as cfg
    import agent_takkub.shared_dev_tools as sdt

    monkeypatch.setattr(cfg, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(sdt, "SHARED_MCP_FILE", tmp_path / "shared-mcp.json")
    monkeypatch.setattr(sdt, "RUNTIME_DIR", tmp_path)

    ok, _ = write_shared_mcp_config("test-token-abc123", "https://api.example.com/pms/mcp")
    assert ok
    return tmp_path


# ---------------------------------------------------------------------------
# render_lead_mcp_config
# ---------------------------------------------------------------------------


class TestRenderLeadMcpConfig:
    def test_returns_path(self, mcp_env):
        result = render_lead_mcp_config()
        assert isinstance(result, (str, pathlib.Path))

    def test_file_is_created(self, mcp_env):
        path = pathlib.Path(render_lead_mcp_config())
        assert path.is_file()

    def test_named_lead_mcp(self, mcp_env):
        path = pathlib.Path(render_lead_mcp_config())
        assert "lead" in path.name.lower()

    def test_contains_write_tools(self, mcp_env):
        path = pathlib.Path(render_lead_mcp_config())
        data = json.loads(path.read_text(encoding="utf-8"))
        allowed = set(data["permissions"]["allow"])
        assert _WRITE_TOOLS <= allowed, f"Missing write tools: {_WRITE_TOOLS - allowed}"

    def test_contains_read_tools(self, mcp_env):
        path = pathlib.Path(render_lead_mcp_config())
        data = json.loads(path.read_text(encoding="utf-8"))
        allowed = set(data["permissions"]["allow"])
        assert _READ_TOOLS <= allowed, f"Missing read tools: {_READ_TOOLS - allowed}"

    def test_contains_browser_mcps(self, mcp_env):
        path = pathlib.Path(render_lead_mcp_config())
        data = json.loads(path.read_text(encoding="utf-8"))
        servers = data.get("mcpServers", {})
        for name in BROWSER_MCPS:
            assert name in servers, f"Browser MCP {name!r} missing from lead config"

    def test_preserves_pms_bearer(self, mcp_env):
        path = pathlib.Path(render_lead_mcp_config())
        data = json.loads(path.read_text(encoding="utf-8"))
        auth = data["mcpServers"]["pms"]["headers"]["Authorization"]
        assert "test-token-abc123" in auth

    def test_returns_none_when_no_pms_configured(self, tmp_path, monkeypatch):
        """When shared-mcp.json doesn't exist, return None (can't render without token)."""
        import agent_takkub.config as cfg
        import agent_takkub.shared_dev_tools as sdt

        monkeypatch.setattr(cfg, "RUNTIME_DIR", tmp_path)
        monkeypatch.setattr(sdt, "SHARED_MCP_FILE", tmp_path / "shared-mcp.json")
        monkeypatch.setattr(sdt, "RUNTIME_DIR", tmp_path)

        result = render_lead_mcp_config()
        assert result is None

    def test_idempotent(self, mcp_env):
        """Calling twice returns same path and file is valid JSON both times."""
        p1 = pathlib.Path(render_lead_mcp_config())
        p2 = pathlib.Path(render_lead_mcp_config())
        assert p1 == p2
        data = json.loads(p1.read_text(encoding="utf-8"))
        assert "permissions" in data


# ---------------------------------------------------------------------------
# render_teammate_mcp_config
# ---------------------------------------------------------------------------


class TestRenderTeammateMcpConfig:
    def test_returns_path(self, mcp_env):
        result = render_teammate_mcp_config()
        assert isinstance(result, (str, pathlib.Path))

    def test_file_is_created(self, mcp_env):
        path = pathlib.Path(render_teammate_mcp_config())
        assert path.is_file()

    def test_named_teammate_mcp(self, mcp_env):
        path = pathlib.Path(render_teammate_mcp_config())
        assert "teammate" in path.name.lower()

    def test_no_write_tools(self, mcp_env):
        path = pathlib.Path(render_teammate_mcp_config())
        data = json.loads(path.read_text(encoding="utf-8"))
        allowed = set(data["permissions"]["allow"])
        leaked = _WRITE_TOOLS & allowed
        assert not leaked, f"Write tools must not appear in teammate config: {leaked}"

    def test_contains_read_tools(self, mcp_env):
        path = pathlib.Path(render_teammate_mcp_config())
        data = json.loads(path.read_text(encoding="utf-8"))
        allowed = set(data["permissions"]["allow"])
        assert _READ_TOOLS <= allowed, f"Missing read tools: {_READ_TOOLS - allowed}"

    def test_contains_browser_mcps(self, mcp_env):
        path = pathlib.Path(render_teammate_mcp_config())
        data = json.loads(path.read_text(encoding="utf-8"))
        servers = data.get("mcpServers", {})
        for name in BROWSER_MCPS:
            assert name in servers, f"Browser MCP {name!r} missing from teammate config"

    def test_preserves_pms_bearer(self, mcp_env):
        path = pathlib.Path(render_teammate_mcp_config())
        data = json.loads(path.read_text(encoding="utf-8"))
        auth = data["mcpServers"]["pms"]["headers"]["Authorization"]
        assert "test-token-abc123" in auth

    def test_returns_none_when_no_pms_configured(self, tmp_path, monkeypatch):
        import agent_takkub.config as cfg
        import agent_takkub.shared_dev_tools as sdt

        monkeypatch.setattr(cfg, "RUNTIME_DIR", tmp_path)
        monkeypatch.setattr(sdt, "SHARED_MCP_FILE", tmp_path / "shared-mcp.json")
        monkeypatch.setattr(sdt, "RUNTIME_DIR", tmp_path)

        result = render_teammate_mcp_config()
        assert result is None

    def test_different_file_from_lead(self, mcp_env):
        lead_path = pathlib.Path(render_lead_mcp_config())
        tm_path = pathlib.Path(render_teammate_mcp_config())
        assert lead_path != tm_path

    def test_idempotent(self, mcp_env):
        p1 = pathlib.Path(render_teammate_mcp_config())
        p2 = pathlib.Path(render_teammate_mcp_config())
        assert p1 == p2
        data = json.loads(p1.read_text(encoding="utf-8"))
        assert "permissions" in data
