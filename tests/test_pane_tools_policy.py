"""Tests for pane_tools_policy: role-aware MCP and plugin policy system."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_takkub import pane_tools_policy


@pytest.fixture
def policy_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect pane_tools_policy.PANE_TOOLS_POLICY_FILE to tmp."""
    policy_file = tmp_path / "pane-tools.json"
    monkeypatch.setattr(pane_tools_policy, "PANE_TOOLS_POLICY_FILE", policy_file)
    return policy_file


class TestLoadPolicy:
    def test_returns_empty_dict_when_file_missing(self, policy_file: Path) -> None:
        assert not policy_file.exists()
        result = pane_tools_policy.load_policy()
        assert result == {}

    def test_returns_empty_dict_on_corrupt_json(self, policy_file: Path) -> None:
        policy_file.write_text("{invalid json")
        result = pane_tools_policy.load_policy()
        assert result == {}

    def test_returns_empty_dict_when_root_not_dict(self, policy_file: Path) -> None:
        policy_file.write_text("[]")
        result = pane_tools_policy.load_policy()
        assert result == {}

    def test_returns_empty_dict_when_roles_missing(self, policy_file: Path) -> None:
        policy_file.write_text('{"version": 1}')
        result = pane_tools_policy.load_policy()
        assert result == {}

    def test_loads_valid_policy(self, policy_file: Path) -> None:
        payload = {
            "version": 1,
            "roles": {
                "qa": {"mcps": ["playwright"], "plugins": ["pordee"]},
                "frontend": {"mcps": [], "plugins": ["claude-plugins-official"]},
            },
        }
        policy_file.write_text(json.dumps(payload), encoding="utf-8")
        result = pane_tools_policy.load_policy()
        # load_policy returns only the roles dict, not the full payload
        assert result.get("qa") == {"mcps": ["playwright"], "plugins": ["pordee"]}
        assert result.get("frontend") == {"mcps": [], "plugins": ["claude-plugins-official"]}

    def test_filters_unknown_roles(self, policy_file: Path) -> None:
        payload = {
            "version": 1,
            "roles": {
                "qa": {"mcps": ["playwright"], "plugins": []},
                "unknown-role": {"mcps": [], "plugins": []},
            },
        }
        policy_file.write_text(json.dumps(payload), encoding="utf-8")
        result = pane_tools_policy.load_policy()
        assert "qa" in result
        assert "unknown-role" not in result

    def test_filters_invalid_item_names(self, policy_file: Path) -> None:
        payload = {
            "version": 1,
            "roles": {
                "qa": {"mcps": ["playwright", "invalid name!"], "plugins": []},
            },
        }
        policy_file.write_text(json.dumps(payload), encoding="utf-8")
        result = pane_tools_policy.load_policy()
        # Invalid names are filtered; valid ones remain
        assert "qa" in result

    def test_accepts_analyst_security_docs_roles(self, policy_file: Path) -> None:
        payload = {
            "version": 1,
            "roles": {
                "analyst": {"mcps": [], "plugins": []},
                "security": {"mcps": [], "plugins": []},
                "docs": {"mcps": [], "plugins": []},
            },
        }
        policy_file.write_text(json.dumps(payload), encoding="utf-8")
        result = pane_tools_policy.load_policy()
        assert set(result) == {"analyst", "security", "docs"}

    def test_skips_role_with_invalid_format(self, policy_file: Path) -> None:
        payload = {
            "version": 1,
            "roles": {
                "qa": "not a dict",
                "frontend": {"mcps": [], "plugins": []},
            },
        }
        policy_file.write_text(json.dumps(payload), encoding="utf-8")
        result = pane_tools_policy.load_policy()
        assert "qa" not in result
        assert "frontend" in result


class TestSavePolicy:
    def test_creates_file_on_success(self, policy_file: Path) -> None:
        payload = {
            "qa": {"mcps": ["playwright"], "plugins": []},
        }
        assert pane_tools_policy.save_policy(payload)
        assert policy_file.exists()

    def test_writes_valid_schema(self, policy_file: Path) -> None:
        payload = {
            "qa": {"mcps": ["playwright"], "plugins": ["pordee"]},
        }
        pane_tools_policy.save_policy(payload)
        data = json.loads(policy_file.read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert data["roles"]["qa"]["mcps"] == ["playwright"]

    def test_rejects_unknown_role(self, policy_file: Path) -> None:
        payload = {
            "unknown": {"mcps": [], "plugins": []},
        }
        assert not pane_tools_policy.save_policy(payload)
        assert not policy_file.exists()

    def test_rejects_invalid_item_name(self, policy_file: Path) -> None:
        payload = {
            "qa": {"mcps": ["invalid name!"], "plugins": []},
        }
        assert not pane_tools_policy.save_policy(payload)
        assert not policy_file.exists()

    def test_rejects_missing_key(self, policy_file: Path) -> None:
        payload = {
            "qa": {"mcps": []},  # missing "plugins"
        }
        assert not pane_tools_policy.save_policy(payload)
        assert not policy_file.exists()

    def test_atomic_write_via_tmp_replace(self, policy_file: Path, tmp_path: Path) -> None:
        # Verify tmp file is used (not left behind on success)
        payload = {
            "qa": {"mcps": [], "plugins": []},
        }
        pane_tools_policy.save_policy(payload)
        # Only the final file should exist
        assert policy_file.exists()
        tmp_files = list(policy_file.parent.glob("*.json.tmp"))
        assert len(tmp_files) == 0  # No tmp leftover


class TestEffectiveMcps:
    def test_returns_default_when_role_not_in_policy(self, policy_file: Path) -> None:
        default = frozenset({"obsidian-vault"})
        result = pane_tools_policy.effective_mcps("backend", default)
        assert result == default

    def test_returns_override_from_policy(self, policy_file: Path) -> None:
        payload = {
            "version": 1,
            "roles": {
                "qa": {"mcps": ["playwright", "chrome-devtools"], "plugins": []},
            },
        }
        policy_file.write_text(json.dumps(payload), encoding="utf-8")
        result = pane_tools_policy.effective_mcps("qa", frozenset({"obsidian-vault"}))
        assert result == frozenset({"playwright", "chrome-devtools"})

    def test_returns_none_when_no_default(self, policy_file: Path) -> None:
        # None = "no policy anywhere" — must NOT collapse to frozenset(),
        # otherwise empty-allowlist and no-policy become indistinguishable.
        assert pane_tools_policy.effective_mcps("frontend", None) is None

    def test_override_takes_precedence_over_default(self, policy_file: Path) -> None:
        payload = {
            "version": 1,
            "roles": {
                "qa": {"mcps": ["playwright"], "plugins": []},
            },
        }
        policy_file.write_text(json.dumps(payload), encoding="utf-8")
        default = frozenset({"obsidian-vault", "chrome-devtools"})
        result = pane_tools_policy.effective_mcps("qa", default)
        # Override replaces default, not merged
        assert result == frozenset({"playwright"})


class TestEffectivePlugins:
    def test_returns_default_when_role_not_in_policy(self, policy_file: Path) -> None:
        default = frozenset({"pordee"})
        result = pane_tools_policy.effective_plugins("backend", default)
        assert result == default

    def test_returns_override_from_policy(self, policy_file: Path) -> None:
        payload = {
            "version": 1,
            "roles": {
                "qa": {"mcps": [], "plugins": ["claude-plugins-official"]},
            },
        }
        policy_file.write_text(json.dumps(payload), encoding="utf-8")
        result = pane_tools_policy.effective_plugins("qa", frozenset({"pordee"}))
        assert result == frozenset({"claude-plugins-official"})

    def test_returns_none_when_no_default(self, policy_file: Path) -> None:
        assert pane_tools_policy.effective_plugins("frontend", None) is None


class TestSetRoleItems:
    def test_creates_new_role_in_policy(self, policy_file: Path) -> None:
        assert pane_tools_policy.set_role_items("qa", "mcps", ["playwright"])
        policy = pane_tools_policy.load_policy()
        assert policy["qa"]["mcps"] == ["playwright"]

    def test_updates_existing_role(self, policy_file: Path) -> None:
        payload = {
            "version": 1,
            "roles": {
                "qa": {"mcps": ["old-mcp"], "plugins": []},
            },
        }
        policy_file.write_text(json.dumps(payload), encoding="utf-8")
        assert pane_tools_policy.set_role_items("qa", "mcps", ["new-mcp"])
        policy = pane_tools_policy.load_policy()
        assert policy["qa"]["mcps"] == ["new-mcp"]

    def test_rejects_invalid_kind(self, policy_file: Path) -> None:
        assert not pane_tools_policy.set_role_items("qa", "invalid", [])

    def test_rejects_unknown_role(self, policy_file: Path) -> None:
        assert not pane_tools_policy.set_role_items("unknown", "mcps", [])

    def test_rejects_invalid_name(self, policy_file: Path) -> None:
        assert not pane_tools_policy.set_role_items("qa", "mcps", ["invalid name!"])


class TestAllowItem:
    def test_adds_item_to_new_role(self, policy_file: Path) -> None:
        assert pane_tools_policy.allow_item("qa", "mcps", "playwright")
        policy = pane_tools_policy.load_policy()
        assert "playwright" in policy["qa"]["mcps"]

    def test_adds_item_to_existing_role(self, policy_file: Path) -> None:
        payload = {
            "version": 1,
            "roles": {
                "qa": {"mcps": ["playwright"], "plugins": []},
            },
        }
        policy_file.write_text(json.dumps(payload), encoding="utf-8")
        assert pane_tools_policy.allow_item("qa", "mcps", "chrome-devtools")
        policy = pane_tools_policy.load_policy()
        assert set(policy["qa"]["mcps"]) == {"playwright", "chrome-devtools"}

    def test_idempotent_when_already_present(self, policy_file: Path) -> None:
        payload = {
            "version": 1,
            "roles": {
                "qa": {"mcps": ["playwright"], "plugins": []},
            },
        }
        policy_file.write_text(json.dumps(payload), encoding="utf-8")
        assert pane_tools_policy.allow_item("qa", "mcps", "playwright")
        policy = pane_tools_policy.load_policy()
        # Only one entry, not duplicated
        assert policy["qa"]["mcps"] == ["playwright"]

    def test_rejects_invalid_name(self, policy_file: Path) -> None:
        assert not pane_tools_policy.allow_item("qa", "mcps", "invalid name!")


class TestDenyItem:
    def test_removes_item_from_existing_role(self, policy_file: Path) -> None:
        payload = {
            "version": 1,
            "roles": {
                "qa": {"mcps": ["playwright", "chrome-devtools"], "plugins": []},
            },
        }
        policy_file.write_text(json.dumps(payload), encoding="utf-8")
        assert pane_tools_policy.deny_item("qa", "mcps", "playwright")
        policy = pane_tools_policy.load_policy()
        assert policy["qa"]["mcps"] == ["chrome-devtools"]

    def test_idempotent_when_already_absent(self, policy_file: Path) -> None:
        payload = {
            "version": 1,
            "roles": {
                "qa": {"mcps": ["playwright"], "plugins": []},
            },
        }
        policy_file.write_text(json.dumps(payload), encoding="utf-8")
        assert pane_tools_policy.deny_item("qa", "mcps", "chrome-devtools")
        policy = pane_tools_policy.load_policy()
        assert policy["qa"]["mcps"] == ["playwright"]

    def test_idempotent_when_role_not_present(self, policy_file: Path) -> None:
        assert pane_tools_policy.deny_item("qa", "mcps", "playwright")
        policy = pane_tools_policy.load_policy()
        # Role not created if it didn't exist
        assert "qa" not in policy or policy["qa"]["mcps"] == []

    def test_rejects_invalid_name(self, policy_file: Path) -> None:
        assert not pane_tools_policy.deny_item("qa", "mcps", "invalid name!")


class TestResetRole:
    def test_removes_role_from_policy(self, policy_file: Path) -> None:
        payload = {
            "version": 1,
            "roles": {
                "qa": {"mcps": ["playwright"], "plugins": []},
            },
        }
        policy_file.write_text(json.dumps(payload), encoding="utf-8")
        assert pane_tools_policy.reset_role("qa")
        policy = pane_tools_policy.load_policy()
        assert "qa" not in policy

    def test_idempotent_when_role_not_present(self, policy_file: Path) -> None:
        assert pane_tools_policy.reset_role("qa")
        # Still succeeds even if qa wasn't in file

    def test_rejects_unknown_role(self, policy_file: Path) -> None:
        assert not pane_tools_policy.reset_role("unknown-role")

    def test_preserves_other_roles(self, policy_file: Path) -> None:
        payload = {
            "version": 1,
            "roles": {
                "qa": {"mcps": ["playwright"], "plugins": []},
                "frontend": {"mcps": [], "plugins": ["pordee"]},
            },
        }
        policy_file.write_text(json.dumps(payload), encoding="utf-8")
        assert pane_tools_policy.reset_role("qa")
        policy = pane_tools_policy.load_policy()
        assert "qa" not in policy
        assert "frontend" in policy


class TestValidateName:
    def test_accepts_valid_names(self) -> None:
        assert pane_tools_policy._validate_name("playwright")
        assert pane_tools_policy._validate_name("chrome-devtools")
        assert pane_tools_policy._validate_name("pordee-tool")
        assert pane_tools_policy._validate_name("tool123")
        assert pane_tools_policy._validate_name("T123")  # case-insensitive

    def test_rejects_invalid_names(self) -> None:
        assert not pane_tools_policy._validate_name("")
        assert not pane_tools_policy._validate_name("-playwright")  # starts with dash
        assert not pane_tools_policy._validate_name("_playwright")  # starts with underscore
        assert not pane_tools_policy._validate_name("playwright!")
        assert not pane_tools_policy._validate_name("playwright tool")  # space


class TestNoneVsEmptyContract:
    """The None-vs-empty distinction that keeps role MCP filtering honest.

    Regression for the fan-out review bug: `effective_mcps` collapsing
    "explicit empty allowlist" and "no policy anywhere" into frozenset()
    made shared_mcp_config_path_for_role's truthiness check fall through
    to the FULL master config for lean roles — the exact inverse of the
    lean-pane intent.
    """

    def test_override_returns_frozenset_even_when_empty(self, policy_file: Path) -> None:
        payload = {"version": 1, "roles": {"lead": {"mcps": [], "plugins": []}}}
        policy_file.write_text(json.dumps(payload), encoding="utf-8")
        got = pane_tools_policy.effective_mcps("lead", None)
        assert got == frozenset()
        assert got is not None

    def test_no_policy_propagates_none_default(self, policy_file: Path) -> None:
        assert pane_tools_policy.effective_mcps("gemini", None) is None
        assert pane_tools_policy.effective_plugins("gemini", None) is None

    def test_no_override_returns_default_verbatim(self, policy_file: Path) -> None:
        default = frozenset({"playwright"})
        assert pane_tools_policy.effective_mcps("qa", default) == default
        assert pane_tools_policy.effective_plugins("qa", default) == default


class TestVariantIntegration:
    """pane-tools.json overrides must actually reach the role variant files
    and the per-role config path — end-to-end through shared_dev_tools."""

    @pytest.fixture
    def mcp_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        from agent_takkub import shared_dev_tools as sdt

        master = tmp_path / "shared-mcp.json"
        master.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "playwright": {"type": "stdio", "command": "npx", "args": []},
                        "chrome-devtools": {"type": "stdio", "command": "npx", "args": []},
                        "custom-tool": {"type": "stdio", "command": "npx", "args": []},
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(sdt, "SHARED_MCP_FILE", master)
        return master

    def test_empty_allowlist_role_skips_mcp_config(self, policy_file: Path, mcp_env: Path) -> None:
        # lead's built-in default is now an EMPTY set → variant is empty →
        # path must be None (skip --mcp-config), NOT the master fallback.
        from agent_takkub import shared_dev_tools as sdt

        sdt._write_role_variants()
        assert sdt.shared_mcp_config_path_for_role("lead") is None

    def test_file_override_reflected_in_variant(self, policy_file: Path, mcp_env: Path) -> None:
        from agent_takkub import shared_dev_tools as sdt

        payload = {
            "version": 1,
            "roles": {"backend": {"mcps": ["custom-tool"], "plugins": []}},
        }
        policy_file.write_text(json.dumps(payload), encoding="utf-8")
        sdt._write_role_variants()
        path = sdt.shared_mcp_config_path_for_role("backend")
        assert path is not None
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert set(data["mcpServers"]) == {"custom-tool"}

    def test_role_without_any_policy_falls_back_to_master(
        self, policy_file: Path, mcp_env: Path
    ) -> None:
        from agent_takkub import shared_dev_tools as sdt

        sdt._write_role_variants()
        # gemini has no built-in entry and no file override → master passthrough.
        assert sdt.shared_mcp_config_path_for_role("gemini") == str(mcp_env)
