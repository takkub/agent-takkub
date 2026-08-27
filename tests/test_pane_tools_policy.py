"""Tests for pane_tools_policy: role-aware MCP and plugin policy system."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_takkub import pane_tools_policy, shared_dev_tools


@pytest.fixture
def policy_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect pane_tools_policy.PANE_TOOLS_POLICY_FILE to tmp.

    Also stubs `shared_dev_tools.regen_role_variants` — `save_policy()`
    fires it on every successful write (#364 lever 4, keeps the on-disk
    MCP variant files from going stale) via a local import, so the REAL
    function would otherwise read the project's real `runtime/shared-
    mcp.json` and write real `runtime/shared-mcp-<role>.json` variant
    files as a side effect of this test — a shared file this suite must
    never touch, same reasoning as redirecting `PANE_TOOLS_POLICY_FILE`
    itself.
    """
    policy_file = tmp_path / "pane-tools.json"
    monkeypatch.setattr(pane_tools_policy, "PANE_TOOLS_POLICY_FILE", policy_file)
    monkeypatch.setattr(shared_dev_tools, "regen_role_variants", lambda: 0)
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

    def test_accepts_registered_custom_role(self, policy_file: Path) -> None:
        from agent_takkub import roles as roles_mod

        role = roles_mod.Role(
            name="analyst-test", label="Analyst", color="#112233", column=2, row=50
        )
        roles_mod.register_role(role)
        try:
            payload = {
                "version": 1,
                "roles": {"analyst-test": {"mcps": [], "plugins": []}},
            }
            policy_file.write_text(json.dumps(payload), encoding="utf-8")
            result = pane_tools_policy.load_policy()
            assert set(result) == {"analyst-test"}
        finally:
            roles_mod.unregister_role("analyst-test")

    def test_rejects_role_never_registered_anywhere(self, policy_file: Path) -> None:
        # "analyst"/"security"/"docs" used to be hand-listed as "known" here
        # despite never being a real role (no roles.py entry, never
        # spawnable) — KNOWN_ROLES now derives from the registry, so an
        # unregistered name is correctly rejected instead of silently
        # accepted forever.
        payload = {
            "version": 1,
            "roles": {"never-registered-ghost-role": {"mcps": [], "plugins": []}},
        }
        policy_file.write_text(json.dumps(payload), encoding="utf-8")
        result = pane_tools_policy.load_policy()
        assert set(result) == set()

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


class TestSavePolicyRegeneratesVariants:
    """#364 lever 4: a policy write must not leave the on-disk
    `shared-mcp-<role>.json` variant — what a pane's `--mcp-config` actually
    reads at spawn time — stale. Before this, only callers that remembered
    to call `shared_dev_tools.regen_role_variants()` themselves kept the two
    in sync; a `pane-tools.json` edit reaching `save_policy()` any other way
    left the previous grant in place (real spawned RAM: a browser MCP
    revoked from a role kept spawning its node/Chromium subprocess tree
    until something else triggered a regen)."""

    def test_save_policy_regenerates_stale_variant(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json as _json

        from agent_takkub import shared_dev_tools

        policy_file = tmp_path / "pane-tools.json"
        shared_mcp_file = tmp_path / "shared-mcp.json"
        monkeypatch.setattr(pane_tools_policy, "PANE_TOOLS_POLICY_FILE", policy_file)
        monkeypatch.setattr(shared_dev_tools, "SHARED_MCP_FILE", shared_mcp_file)

        shared_mcp_file.write_text(
            _json.dumps(
                {
                    "mcpServers": {
                        "graft": {"type": "stdio", "command": "npx", "args": ["graft"]},
                        "context7": {"type": "stdio", "command": "npx", "args": ["context7"]},
                    }
                }
            ),
            encoding="utf-8",
        )
        # Old grant on disk (as if written by a previous policy/regen).
        variant = shared_dev_tools._role_variant_path("backend")
        variant.write_text(
            _json.dumps({"mcpServers": {"graft": {"command": "npx", "args": ["graft"]}}}),
            encoding="utf-8",
        )

        ok = pane_tools_policy.save_policy({"backend": {"mcps": ["context7"], "plugins": []}})

        assert ok
        data = _json.loads(variant.read_text(encoding="utf-8"))
        assert set(data["mcpServers"]) == {"context7"}, (
            "save_policy() must regenerate the on-disk variant itself — "
            "not rely on the caller to remember"
        )


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

    def test_materializes_default_when_role_not_present(self, policy_file: Path) -> None:
        """#414: denying an item that only comes from the BUILT-IN default
        (no override yet) must actually remove it from the effective
        allowlist, not silently no-op just because there was no file entry
        to edit yet."""
        assert pane_tools_policy.deny_item("qa", "mcps", "playwright")
        policy = pane_tools_policy.load_policy()
        assert "qa" in policy
        assert "playwright" not in policy["qa"]["mcps"]
        # The role's other default MCPs are preserved, not wiped out.
        assert set(policy["qa"]["mcps"]) == {"chrome-devtools", "graft"}
        effective = pane_tools_policy.effective_mcps("qa")
        assert effective is not None
        assert "playwright" not in effective

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
        assert pane_tools_policy._validate_name("www.abc.com")

    def test_rejects_invalid_names(self) -> None:
        assert not pane_tools_policy._validate_name("")
        assert not pane_tools_policy._validate_name("-playwright")  # starts with dash
        assert not pane_tools_policy._validate_name("_playwright")  # starts with underscore
        assert not pane_tools_policy._validate_name("playwright!")
        assert not pane_tools_policy._validate_name("playwright tool")  # space
        assert not pane_tools_policy._validate_name("..")
        assert not pane_tools_policy._validate_name(".hidden")
        assert not pane_tools_policy._validate_name("trailing.")
        assert not pane_tools_policy._validate_name("../../etc/passwd")


class TestKnownRoles:
    """A6: known_roles() unions known_roles_base() with any registered
    custom role, so custom roles can get an MCP/plugin policy entry written
    the same way a built-in role can."""

    @pytest.fixture
    def custom_role_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        from agent_takkub import custom_roles

        monkeypatch.setattr(custom_roles, "CUSTOM_ROLES_FILE", tmp_path / "custom-roles.json")
        monkeypatch.setattr(custom_roles, "CUSTOM_AGENTS_DIR", tmp_path / "agents")
        return tmp_path

    def test_defaults_to_static_known_roles_when_custom_module_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_takkub import custom_roles

        def boom():
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(custom_roles, "list_role_names", boom)
        assert pane_tools_policy.known_roles() == pane_tools_policy.known_roles_base()

    def test_includes_registered_custom_role(self, custom_role_files: Path) -> None:
        from agent_takkub import custom_roles

        custom_roles.create_role("data-eng", "Data Eng", "#112233", 1, 5, "x")
        known = pane_tools_policy.known_roles()
        assert "data-eng" in known
        assert pane_tools_policy.known_roles_base() < known

    def test_set_role_items_accepts_custom_role(
        self, policy_file: Path, custom_role_files: Path
    ) -> None:
        from agent_takkub import custom_roles

        custom_roles.create_role("data-eng", "Data Eng", "#112233", 1, 5, "x")
        assert pane_tools_policy.set_role_items("data-eng", "mcps", ["playwright"]) is True
        assert pane_tools_policy.load_policy()["data-eng"]["mcps"] == ["playwright"]

    def test_set_role_items_rejects_unregistered_role(self, policy_file: Path) -> None:
        assert pane_tools_policy.set_role_items("totally-unknown", "mcps", []) is False


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

    def test_registered_role_without_any_policy_denies_not_master(
        self, policy_file: Path, mcp_env: Path
    ) -> None:
        # gemini is a REGISTERED role (roles.all_role_names()) with no
        # built-in _ROLE_MCP_POLICY entry and no pane-tools.json override.
        # It used to fall back to the master file (every shared MCP) —
        # docs/reviews/2026-08-05-graft-mcp-security.md M1 closed that: a
        # registered role with no explicit policy is deny-by-default, same
        # as an explicit empty allowlist, not legacy passthrough.
        from agent_takkub import shared_dev_tools as sdt

        sdt._write_role_variants()
        assert sdt.shared_mcp_config_path_for_role("gemini") is None

    def test_unregistered_role_name_still_falls_back_to_master(
        self, policy_file: Path, mcp_env: Path
    ) -> None:
        # A name the cockpit has never registered at all (typo, stale
        # config) is not a policy gap — `role_mcp_allowlist` returns None
        # for it and the legacy master-passthrough contract still applies.
        from agent_takkub import shared_dev_tools as sdt

        sdt._write_role_variants()
        assert sdt.shared_mcp_config_path_for_role("totally-unregistered-role-xyz") == str(mcp_env)
