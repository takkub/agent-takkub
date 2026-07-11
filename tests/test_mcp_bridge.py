"""Tests for `mcp_bridge.py` (issue #100 — per-provider MCP injection adapter).

Covers the pure TOML-literal encoder plus each provider's argv translation.
Claude's path only needs a smoke test (it's a thin pass-through to the
pre-existing `shared_dev_tools.browser_profile_mcp_config_path` +
`--mcp-config`/`--strict-mcp-config`, already covered by test_browser_mcps.py);
the bulk of the new-behaviour coverage is codex's `-c` override translation.
"""

from __future__ import annotations

import json

import pytest

from agent_takkub import mcp_bridge


@pytest.fixture
def isolated_mcp_file(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Redirect shared_dev_tools' SHARED_MCP_FILE + pane_tools_policy's
    PANE_TOOLS_POLICY_FILE to tmp paths — same isolation test_browser_mcps.py
    uses, so a real dev machine's runtime/shared-mcp.json or
    ~/.takkub/pane-tools.json never leaks into these assertions."""
    from agent_takkub import pane_tools_policy as ptp
    from agent_takkub import shared_dev_tools as sdt

    target = tmp_path / "shared-mcp.json"
    monkeypatch.setattr(sdt, "SHARED_MCP_FILE", target)
    monkeypatch.setattr(ptp, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")
    return target


def _write_master(path, servers: dict) -> None:
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


class TestTomlLiteral:
    def test_string_escapes_backslash_and_quote(self):
        assert mcp_bridge._toml_literal("C:\\Users\\x") == '"C:\\\\Users\\\\x"'
        assert mcp_bridge._toml_literal('say "hi"') == '"say \\"hi\\""'

    def test_list_of_strings(self):
        assert mcp_bridge._toml_literal(["-y", "@scope/pkg@1.2.3"]) == '["-y","@scope/pkg@1.2.3"]'

    def test_dict_inline_table(self):
        assert mcp_bridge._toml_literal({"FOO": "bar"}) == '{FOO="bar"}'

    def test_bool_and_number(self):
        assert mcp_bridge._toml_literal(True) == "true"
        assert mcp_bridge._toml_literal(False) == "false"
        assert mcp_bridge._toml_literal(60) == "60"

    def test_unsupported_type_raises(self):
        with pytest.raises(TypeError):
            mcp_bridge._toml_literal(object())


class TestCodexMcpArgv:
    def test_no_master_file_returns_empty(self, isolated_mcp_file):
        assert mcp_bridge.mcp_argv_for_provider("codex", "codex", None, "proj") == []

    def test_translates_command_args_env(self, isolated_mcp_file):
        _write_master(
            isolated_mcp_file,
            {
                "demo": {
                    "type": "stdio",
                    "command": "node",
                    "args": ["-e", "1"],
                    "env": {"FOO": "1"},
                }
            },
        )
        argv = mcp_bridge.mcp_argv_for_provider("codex", "codex", None, "proj")
        pairs = list(zip(argv[0::2], argv[1::2], strict=True))
        assert pairs == [
            ("-c", 'mcp_servers.demo.command="node"'),
            ("-c", 'mcp_servers.demo.args=["-e","1"]'),
            ("-c", 'mcp_servers.demo.env={FOO="1"}'),
        ]
        # "type" has no codex equivalent — never forwarded.
        assert not any("type" in tok for tok in argv)

    def test_role_with_empty_policy_gets_no_overrides(self, isolated_mcp_file):
        from agent_takkub import pane_tools_policy as ptp
        from agent_takkub import shared_dev_tools as sdt

        _write_master(isolated_mcp_file, {"demo": {"command": "node"}})
        ptp.set_role_items("backend", "mcps", [])  # explicit empty allowlist
        sdt.regen_role_variants()  # generates the empty shared-mcp-backend.json variant
        assert mcp_bridge.mcp_argv_for_provider("codex", "backend", None, "proj") == []

    def test_never_raises_on_corrupt_master_file(self, isolated_mcp_file):
        isolated_mcp_file.write_text("{not json", encoding="utf-8")
        assert mcp_bridge.mcp_argv_for_provider("codex", "codex", None, "proj") == []


class TestClaudeMcpArgv:
    def test_no_master_file_returns_empty(self, isolated_mcp_file):
        assert mcp_bridge.mcp_argv_for_provider("claude", "qa", None, "proj") == []

    def test_returns_strict_mcp_config_pair(self, isolated_mcp_file):
        _write_master(isolated_mcp_file, {"playwright": {"command": "npx", "args": []}})
        argv = mcp_bridge.mcp_argv_for_provider("claude", "qa", None, "proj")
        assert argv[0] == "--mcp-config"
        assert argv[2] == "--strict-mcp-config"


class TestGeminiMcpArgv:
    def test_always_empty_documented_no_op(self, isolated_mcp_file):
        _write_master(isolated_mcp_file, {"playwright": {"command": "npx", "args": []}})
        assert mcp_bridge.mcp_argv_for_provider("gemini", "qa", None, "proj") == []


class TestUnknownProvider:
    def test_unknown_provider_name_returns_empty(self, isolated_mcp_file):
        assert mcp_bridge.mcp_argv_for_provider("nope", "qa", None, "proj") == []
