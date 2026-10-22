from __future__ import annotations

import json
import sys

from agent_takkub import ccc_store


def test_runner_config_is_provider_neutral() -> None:
    config = ccc_store.runner_mcp_config()
    assert config["command"] == sys.executable
    assert config["args"] == ["-m", "agent_takkub.cli", "ccc", "serve"]


def test_ensure_cursor_mcp_preserves_other_servers(tmp_path) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps({"mcpServers": {"other": {"command": "node"}}}), encoding="utf-8"
    )

    ok, message = ccc_store.ensure_cursor_mcp(config_path)

    assert ok and "added" in message
    servers = json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"]
    assert servers["other"] == {"command": "node"}
    assert servers["ccc"] == ccc_store.runner_mcp_config()


def test_ensure_cursor_mcp_refuses_conflicting_name(tmp_path) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps({"mcpServers": {"ccc": {"command": "other"}}}), encoding="utf-8"
    )

    ok, message = ccc_store.ensure_cursor_mcp(config_path)

    assert not ok
    assert "left unchanged" in message
    assert json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"]["ccc"] == {
        "command": "other"
    }
