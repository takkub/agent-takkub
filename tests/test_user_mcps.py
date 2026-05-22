"""Tests for ensure_user_mcps() pruning logic.

Covers:
- Stale non-browser user MCP is removed when no longer in policy
- Browser MCPs are never pruned (managed by ensure_browser_mcps)
- Entries currently in policy are preserved
- pms stays with TAKKUB_INCLUDE_PMS=1, gets pruned without it
"""

from __future__ import annotations

import json
import pathlib

import pytest

from agent_takkub import shared_dev_tools as sdt
from agent_takkub.shared_dev_tools import (
    _BROWSER_MCP_NAMES,
    BROWSER_MCPS,
    ensure_user_mcps,
)

_OBSIDIAN_CFG = {"type": "stdio", "command": "npx", "args": ["-y", "obsidian-vault-mcp"]}
_POSTGRES_CFG = {"type": "stdio", "command": "npx", "args": ["-y", "postgres-pms-mcp"]}
_PMS_CFG = {
    "type": "http",
    "url": "http://localhost:3001/mcp",
    "headers": {"Authorization": "Bearer secret"},
}
_STALE_CFG = {"type": "stdio", "command": "npx", "args": ["-y", "old-mcp"]}


@pytest.fixture()
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
    """Redirect SHARED_MCP_FILE and ~/.claude.json to tmp paths."""
    mcp_file = tmp_path / "shared-mcp.json"
    claude_json = tmp_path / ".claude.json"

    monkeypatch.setattr(sdt, "SHARED_MCP_FILE", mcp_file)

    # Patch pathlib.Path.home() so ~/.claude.json resolves to our tmp file
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: tmp_path))

    return mcp_file, claude_json


def _write_claude_json(claude_json: pathlib.Path, servers: dict) -> None:
    claude_json.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def _read_mcp(mcp_file: pathlib.Path) -> dict:
    return json.loads(mcp_file.read_text(encoding="utf-8"))


class TestPruneStaleEntries:
    def test_prune_removes_stale_non_browser_mcp(
        self, isolated, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp_file, claude_json = isolated
        # shared-mcp.json has a stale entry "old-mcp" that is no longer in policy
        mcp_file.write_text(
            json.dumps({"mcpServers": {"old-mcp": _STALE_CFG, **BROWSER_MCPS}}),
            encoding="utf-8",
        )
        # ~/.claude.json has only obsidian-vault (no old-mcp)
        _write_claude_json(claude_json, {"obsidian-vault": _OBSIDIAN_CFG})

        ok, msg = ensure_user_mcps()
        assert ok, msg
        data = _read_mcp(mcp_file)
        assert "old-mcp" not in data["mcpServers"]
        assert "pruned" in msg
        assert "old-mcp" in msg

    def test_prune_preserves_browser_mcps(self, isolated, monkeypatch: pytest.MonkeyPatch) -> None:
        mcp_file, claude_json = isolated
        # shared-mcp.json has browser MCPs and a stale user MCP
        mcp_file.write_text(
            json.dumps({"mcpServers": {"stale-thing": _STALE_CFG, **BROWSER_MCPS}}),
            encoding="utf-8",
        )
        _write_claude_json(claude_json, {"obsidian-vault": _OBSIDIAN_CFG})

        ok, msg = ensure_user_mcps()
        assert ok, msg
        data = _read_mcp(mcp_file)
        # All browser MCPs must survive
        for browser_name in _BROWSER_MCP_NAMES:
            assert browser_name in data["mcpServers"], f"{browser_name} was pruned unexpectedly"
        assert "stale-thing" not in data["mcpServers"]

    def test_prune_preserves_entries_in_current_policy(
        self, isolated, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp_file, claude_json = isolated
        # obsidian-vault is already present; stale-old is not in policy
        mcp_file.write_text(
            json.dumps({"mcpServers": {"obsidian-vault": _OBSIDIAN_CFG, "stale-old": _STALE_CFG}}),
            encoding="utf-8",
        )
        _write_claude_json(claude_json, {"obsidian-vault": _OBSIDIAN_CFG})

        ok, msg = ensure_user_mcps()
        assert ok, msg
        data = _read_mcp(mcp_file)
        assert "obsidian-vault" in data["mcpServers"]
        assert "stale-old" not in data["mcpServers"]

    def test_no_write_when_already_up_to_date(
        self, isolated, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp_file, claude_json = isolated
        # Exact match — nothing to change or prune
        mcp_file.write_text(
            json.dumps({"mcpServers": {"obsidian-vault": _OBSIDIAN_CFG}}),
            encoding="utf-8",
        )
        _write_claude_json(claude_json, {"obsidian-vault": _OBSIDIAN_CFG})
        before = mcp_file.read_text(encoding="utf-8")

        ok, msg = ensure_user_mcps()
        assert ok, msg
        assert "already up-to-date" in msg
        assert mcp_file.read_text(encoding="utf-8") == before


class TestPmsOptIn:
    def test_pms_stays_when_env_set(self, isolated, monkeypatch: pytest.MonkeyPatch) -> None:
        mcp_file, claude_json = isolated
        monkeypatch.setenv("TAKKUB_INCLUDE_PMS", "1")
        # pms currently in shared-mcp.json, user has it in ~/.claude.json
        mcp_file.write_text(
            json.dumps({"mcpServers": {"pms": _PMS_CFG}}),
            encoding="utf-8",
        )
        _write_claude_json(claude_json, {"pms": _PMS_CFG, "obsidian-vault": _OBSIDIAN_CFG})

        ok, msg = ensure_user_mcps()
        assert ok, msg
        data = _read_mcp(mcp_file)
        assert "pms" in data["mcpServers"]

    def test_pms_pruned_when_env_not_set(self, isolated, monkeypatch: pytest.MonkeyPatch) -> None:
        mcp_file, claude_json = isolated
        monkeypatch.delenv("TAKKUB_INCLUDE_PMS", raising=False)
        # pms is stale in shared-mcp.json (from a previous TAKKUB_INCLUDE_PMS=1 run)
        mcp_file.write_text(
            json.dumps({"mcpServers": {"pms": _PMS_CFG, "obsidian-vault": _OBSIDIAN_CFG}}),
            encoding="utf-8",
        )
        _write_claude_json(claude_json, {"pms": _PMS_CFG, "obsidian-vault": _OBSIDIAN_CFG})

        ok, msg = ensure_user_mcps()
        assert ok, msg
        data = _read_mcp(mcp_file)
        assert "pms" not in data["mcpServers"]
        assert "obsidian-vault" in data["mcpServers"]
        assert "pruned" in msg
        assert "pms" in msg

    def test_pms_pruned_message_does_not_contain_bearer_value(
        self, isolated, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp_file, claude_json = isolated
        monkeypatch.delenv("TAKKUB_INCLUDE_PMS", raising=False)
        mcp_file.write_text(
            json.dumps({"mcpServers": {"pms": _PMS_CFG}}),
            encoding="utf-8",
        )
        _write_claude_json(claude_json, {"pms": _PMS_CFG})

        ok, msg = ensure_user_mcps()
        assert ok, msg
        # The bearer token value must never appear in the return message
        assert "secret" not in msg
        assert "Bearer" not in msg
