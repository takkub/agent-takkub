"""Characterization + CRUD tests for settings_management's MCP Servers slice.

Ownership: browser MCPs (`shared_dev_tools.BROWSER_MCPS` — playwright,
chrome-devtools) are MANAGED (definition read-only, assignment still
editable from a Role); everything else is USER (full CRUD).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub import pane_tools_policy, shared_dev_tools
from agent_takkub.settings_management.commands import (
    CreateMcpCommand,
    McpConfigDraft,
    UpdateMcpCommand,
)
from agent_takkub.settings_management.models import Ownership
from agent_takkub.settings_management.repositories import mcps as mcps_repo


@pytest.fixture(autouse=True)
def redirect_stores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(shared_dev_tools, "SHARED_MCP_FILE", tmp_path / "shared-mcp.json")
    monkeypatch.setattr(pane_tools_policy, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")
    yield tmp_path


def _draft(**overrides) -> McpConfigDraft:
    base = dict(command="npx", args=["-y", "some-mcp"], env={}, type="stdio")
    base.update(overrides)
    return McpConfigDraft(**base)


class TestOwnership:
    def test_browser_mcp_is_managed_and_read_only(self) -> None:
        shared_dev_tools.ensure_browser_mcps()
        detail = mcps_repo.get("playwright")
        assert detail.ownership is Ownership.MANAGED
        assert detail.capabilities.can_update is False
        assert detail.capabilities.can_delete is False

    def test_user_mcp_is_fully_editable(self) -> None:
        mcps_repo.create(CreateMcpCommand(name="obsidian", config=_draft()))
        detail = mcps_repo.get("obsidian")
        assert detail.ownership is Ownership.USER
        assert detail.capabilities.can_update is True
        assert detail.capabilities.can_delete is True


class TestCreateUpdateDelete:
    def test_create_rejects_name_collision(self) -> None:
        mcps_repo.create(CreateMcpCommand(name="obsidian", config=_draft()))
        result = mcps_repo.create(CreateMcpCommand(name="obsidian", config=_draft()))
        assert not result.ok

    def test_create_cannot_override_browser_mcp_name(self) -> None:
        result = mcps_repo.create(CreateMcpCommand(name="playwright", config=_draft()))
        assert not result.ok

    def test_update_rejects_managed_mcp(self) -> None:
        shared_dev_tools.ensure_browser_mcps()
        result = mcps_repo.update("playwright", UpdateMcpCommand(config=_draft(command="hacked")))
        assert not result.ok

    def test_update_preserves_unknown_config_keys(self) -> None:
        mcps_repo.create(CreateMcpCommand(name="obsidian", config=_draft()))
        # Hand-add a key the form never exposes (mirrors a real "type"-adjacent
        # extra a hand-authored ~/.claude.json entry might carry).
        raw = shared_dev_tools.list_master_mcps()
        raw["obsidian"]["cwd"] = "/some/path"
        shared_dev_tools.SHARED_MCP_FILE.write_text(
            __import__("json").dumps({"mcpServers": raw}), encoding="utf-8"
        )

        result = mcps_repo.update("obsidian", UpdateMcpCommand(config=_draft(command="new-cmd")))
        assert result.ok
        cfg = shared_dev_tools.list_master_mcps()["obsidian"]
        assert cfg["cwd"] == "/some/path"
        assert cfg["command"] == "new-cmd"

    def test_delete_removes_master_entry_and_policy_reference(self) -> None:
        mcps_repo.create(CreateMcpCommand(name="obsidian", config=_draft()))
        pane_tools_policy.set_role_items("backend", "mcps", ["obsidian"])
        plan = mcps_repo.delete_plan("obsidian")
        assert plan.deletable
        assert any("backend" in e for e in plan.effects)
        result = mcps_repo.delete("obsidian", plan.version)
        assert result.ok
        assert "obsidian" not in shared_dev_tools.list_master_mcps()
        assert "obsidian" not in pane_tools_policy.effective_mcps(
            "backend", default=frozenset({"obsidian"})
        )

    def test_delete_stale_plan_version_is_rejected(self) -> None:
        mcps_repo.create(CreateMcpCommand(name="obsidian", config=_draft()))
        plan = mcps_repo.delete_plan("obsidian")
        pane_tools_policy.set_role_items("backend", "mcps", ["obsidian"])
        result = mcps_repo.delete("obsidian", plan.version)
        assert not result.ok

    def test_delete_managed_mcp_is_rejected(self) -> None:
        shared_dev_tools.ensure_browser_mcps()
        plan = mcps_repo.delete_plan("playwright")
        assert not plan.deletable
        result = mcps_repo.delete("playwright", plan.version)
        assert not result.ok


class TestSecretMasking:
    def test_get_masks_env_secret_and_flags_has_secrets(self) -> None:
        mcps_repo.create(
            CreateMcpCommand(name="obsidian", config=_draft(env={"API_TOKEN": "sekrit-value"}))
        )
        detail = mcps_repo.get("obsidian")
        assert detail.has_secrets is True
        assert detail.config["env"]["API_TOKEN"] != "sekrit-value"
        assert "sekrit-value" not in str(detail.config)

    def test_get_does_not_flag_secrets_when_there_are_none(self) -> None:
        mcps_repo.create(CreateMcpCommand(name="obsidian", config=_draft()))
        detail = mcps_repo.get("obsidian")
        assert detail.has_secrets is False


class TestMaskedSecretWriteback:
    """HIGH-1: an unrelated field edit must never clobber a stored
    credential with the masked placeholder — the masked read → unrelated
    edit → update → raw-store round trip the original review flagged as
    untested."""

    def test_editing_unrelated_field_preserves_real_env_secret(self) -> None:
        mcps_repo.create(
            CreateMcpCommand(name="obsidian", config=_draft(env={"API_TOKEN": "sekrit-value"}))
        )
        detail = mcps_repo.get("obsidian")  # masked read, like the real UI does
        assert detail.config["env"]["API_TOKEN"] == "••••••••"

        draft = _draft(command="new-command", env=dict(detail.config["env"]))
        result = mcps_repo.update("obsidian", UpdateMcpCommand(config=draft))
        assert result.ok

        raw = shared_dev_tools.list_master_mcps()["obsidian"]
        assert raw["command"] == "new-command"
        assert raw["env"]["API_TOKEN"] == "sekrit-value"

    def test_editing_the_secret_field_itself_still_takes_the_new_value(self) -> None:
        mcps_repo.create(
            CreateMcpCommand(name="obsidian", config=_draft(env={"API_TOKEN": "old-value"}))
        )
        draft = _draft(env={"API_TOKEN": "brand-new-value"})
        result = mcps_repo.update("obsidian", UpdateMcpCommand(config=draft))
        assert result.ok
        raw = shared_dev_tools.list_master_mcps()["obsidian"]
        assert raw["env"]["API_TOKEN"] == "brand-new-value"

    def test_credential_bearing_dsn_arg_survives_unrelated_edit(self) -> None:
        mcps_repo.create(
            CreateMcpCommand(
                name="obsidian",
                config=_draft(args=["--dsn", "postgres://user:sekrit@host/db"]),
            )
        )
        detail = mcps_repo.get("obsidian")
        assert "sekrit" not in str(detail.config["args"])

        draft = _draft(command="new-command", args=list(detail.config["args"]))
        result = mcps_repo.update("obsidian", UpdateMcpCommand(config=draft))
        assert result.ok
        raw = shared_dev_tools.list_master_mcps()["obsidian"]
        assert raw["args"] == ["--dsn", "postgres://user:sekrit@host/db"]

    def test_secret_header_survives_unrelated_edit(self) -> None:
        mcps_repo.create(CreateMcpCommand(name="obsidian", config=_draft()))
        # Headers aren't form-editable — hand-add one the way an existing
        # ~/.claude.json import might carry, mirroring
        # test_update_preserves_unknown_config_keys's pattern.
        raw = shared_dev_tools.list_master_mcps()
        raw["obsidian"]["headers"] = {"Authorization": "Bearer sekrit-token"}
        shared_dev_tools.SHARED_MCP_FILE.write_text(
            __import__("json").dumps({"mcpServers": raw}), encoding="utf-8"
        )

        result = mcps_repo.update("obsidian", UpdateMcpCommand(config=_draft(command="new-cmd")))
        assert result.ok
        cfg = shared_dev_tools.list_master_mcps()["obsidian"]
        assert cfg["headers"]["Authorization"] == "Bearer sekrit-token"
        assert cfg["command"] == "new-cmd"


class TestMcpRoleVariantRegen:
    """HIGH-4: MCP role-variant regeneration is checked and transactional —
    a failure rolls the master config back too instead of leaving
    master/variants disagreeing."""

    def test_create_regenerates_role_variant(self) -> None:
        pane_tools_policy.set_role_items("backend", "mcps", [])
        mcps_repo.create(CreateMcpCommand(name="obsidian", config=_draft()))
        pane_tools_policy.set_role_items("backend", "mcps", ["obsidian"])
        result = mcps_repo.update("obsidian", UpdateMcpCommand(config=_draft(command="changed")))
        assert result.ok
        variant = shared_dev_tools._role_variant_path("backend")
        data = __import__("json").loads(variant.read_text(encoding="utf-8"))
        assert "obsidian" in data["mcpServers"]
        assert data["mcpServers"]["obsidian"]["command"] == "changed"

    def test_variant_regen_failure_rolls_back_master_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcps_repo.create(CreateMcpCommand(name="obsidian", config=_draft()))
        before = shared_dev_tools.list_master_mcps()

        def boom() -> tuple[bool, list[str]]:
            return False, ["backend"]

        monkeypatch.setattr(shared_dev_tools, "regen_role_variants_checked", boom)
        result = mcps_repo.update(
            "obsidian", UpdateMcpCommand(config=_draft(command="should-not-stick"))
        )
        assert not result.ok
        after = shared_dev_tools.list_master_mcps()
        assert after == before  # master rolled back, not left half-applied


class TestList:
    def test_list_includes_managed_and_user(self) -> None:
        shared_dev_tools.ensure_browser_mcps()
        mcps_repo.create(CreateMcpCommand(name="obsidian", config=_draft()))
        by_name = {m.name: m for m in mcps_repo.list()}
        assert by_name["playwright"].ownership is Ownership.MANAGED
        assert by_name["obsidian"].ownership is Ownership.USER

    def test_list_query_filters_by_name(self) -> None:
        mcps_repo.create(CreateMcpCommand(name="obsidian", config=_draft()))
        mcps_repo.create(CreateMcpCommand(name="other-mcp", config=_draft()))
        names = {m.name for m in mcps_repo.list(query="obsid")}
        assert names == {"obsidian"}
