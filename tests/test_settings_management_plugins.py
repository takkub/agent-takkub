"""Characterization + CRUD tests for settings_management's Plugins slice.

Never shells out to the real ``claude`` CLI — ``plugin_installer``'s
``list_installed``/``list_marketplaces``/``install_by_id``/``uninstall_plugin``
are monkeypatched to an in-memory fake registry, mirroring how
``test_settings_management_mcps.py`` redirects the JSON stores.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub import config, pane_tools_policy, plugin_installer
from agent_takkub.settings_management.commands import CreatePluginCommand
from agent_takkub.settings_management.repositories import plugins as plugins_repo


class _FakeRegistry:
    def __init__(self) -> None:
        self.entries: list[dict] = [
            {
                "id": "github@claude-plugins-official",
                "version": "1.0.0",
                "scope": "user",
                "enabled": True,
                "installPath": "/cache/claude-plugins-official/github/1.0.0",
                "installedAt": "2026-07-01T00:00:00Z",
            },
            {
                "id": "security-guidance@claude-plugins-official",
                "version": "2.0.6",
                "scope": "user",
                "enabled": True,
                "installPath": "/cache/claude-plugins-official/security-guidance/2.0.6",
                "installedAt": "2026-07-01T00:00:00Z",
            },
            {
                "id": "obsidian@claude-obsidian-marketplace",
                "version": "1.4.3",
                "scope": "user",
                "enabled": False,
                "installPath": "/cache/claude-obsidian-marketplace/obsidian/1.4.3",
                "installedAt": "2026-07-01T00:00:00Z",
            },
        ]
        self.marketplaces: list[dict] = [
            {"name": "claude-plugins-official", "repo": "anthropics/claude-plugins-official"},
            {"name": "claude-obsidian-marketplace", "repo": "AgriciDaniel/claude-obsidian"},
        ]
        self.last_install: str | None = None
        self.last_uninstall: str | None = None
        self.install_fails_with: str | None = None
        self.uninstall_fails_with: str | None = None

    def list_installed(self) -> list[dict]:
        return [dict(e) for e in self.entries]

    def list_marketplaces(self) -> list[dict]:
        return [dict(m) for m in self.marketplaces]

    def install_by_id(self, plugin_id: str) -> tuple[bool, str]:
        self.last_install = plugin_id
        if self.install_fails_with:
            return False, self.install_fails_with
        key, _, marketplace = plugin_id.partition("@")
        self.entries.append(
            {
                "id": plugin_id if marketplace else f"{key}@claude-plugins-official",
                "version": "0.1.0",
                "scope": "user",
                "enabled": True,
                "installPath": f"/cache/{marketplace or 'claude-plugins-official'}/{key}/0.1.0",
                "installedAt": "2026-07-11T00:00:00Z",
            }
        )
        return True, "installed"

    def uninstall_plugin(self, plugin_id: str) -> tuple[bool, str]:
        self.last_uninstall = plugin_id
        if self.uninstall_fails_with:
            return False, self.uninstall_fails_with
        self.entries = [e for e in self.entries if e["id"] != plugin_id]
        return True, "uninstalled"


@pytest.fixture(autouse=True)
def redirect_stores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(pane_tools_policy, "PANE_TOOLS_POLICY_FILE", tmp_path / "pane-tools.json")
    monkeypatch.setattr(
        config,
        "_SAFE_PLUGINS",
        ("claude-plugins-official", "claude-obsidian-marketplace"),
    )

    fake = _FakeRegistry()
    monkeypatch.setattr(plugin_installer, "list_installed", fake.list_installed)
    monkeypatch.setattr(plugin_installer, "list_marketplaces", fake.list_marketplaces)
    monkeypatch.setattr(plugin_installer, "install_by_id", fake.install_by_id)
    monkeypatch.setattr(plugin_installer, "uninstall_plugin", fake.uninstall_plugin)
    yield fake


class TestList:
    def test_list_includes_every_installed_plugin(self, redirect_stores: _FakeRegistry) -> None:
        ids = {p.id for p in plugins_repo.list()}
        assert ids == {
            "github@claude-plugins-official",
            "security-guidance@claude-plugins-official",
            "obsidian@claude-obsidian-marketplace",
        }

    def test_list_query_filters_by_id(self, redirect_stores: _FakeRegistry) -> None:
        ids = {p.id for p in plugins_repo.list(query="github")}
        assert ids == {"github@claude-plugins-official"}

    def test_list_flags_denylisted_plugin_as_blocked(self, redirect_stores: _FakeRegistry) -> None:
        by_id = {p.id: p for p in plugins_repo.list()}
        assert by_id["security-guidance@claude-plugins-official"].blocked is True
        assert by_id["github@claude-plugins-official"].blocked is False


class TestGet:
    def test_get_unknown_plugin_raises(self, redirect_stores: _FakeRegistry) -> None:
        with pytest.raises(plugins_repo.PluginNotFoundError):
            plugins_repo.get("nope@nowhere")

    def test_get_blocked_plugin_has_reason_and_no_allowed_roles(
        self, redirect_stores: _FakeRegistry
    ) -> None:
        detail = plugins_repo.get("security-guidance@claude-plugins-official")
        assert detail.blocked is True
        assert detail.blocked_reason
        assert detail.allowed_roles == ()
        assert detail.capabilities.can_update is False
        assert detail.capabilities.can_delete is True

    def test_get_governable_plugin_lists_allowed_roles(
        self, redirect_stores: _FakeRegistry
    ) -> None:
        detail = plugins_repo.get("github@claude-plugins-official")
        assert detail.governable is True
        # frontend defaults to _DESIGN_PLUGINS which includes claude-plugins-official
        assert "frontend" in detail.allowed_roles

    def test_get_ungovernable_marketplace_has_no_allowed_roles(
        self, monkeypatch: pytest.MonkeyPatch, redirect_stores: _FakeRegistry
    ) -> None:
        # Narrow the governable set so claude-obsidian-marketplace drops out.
        monkeypatch.setattr(config, "_SAFE_PLUGINS", ("claude-plugins-official",))
        detail = plugins_repo.get("obsidian@claude-obsidian-marketplace")
        assert detail.governable is False
        assert detail.allowed_roles == ()


class TestCreate:
    def test_create_with_explicit_marketplace_installs_and_returns_id(
        self, redirect_stores: _FakeRegistry
    ) -> None:
        result = plugins_repo.create(
            CreatePluginCommand(key="frontend-design", marketplace="claude-plugins-official")
        )
        assert result.ok
        assert result.entity_id == "frontend-design@claude-plugins-official"
        assert redirect_stores.last_install == "frontend-design@claude-plugins-official"

    def test_create_rejects_empty_key(self, redirect_stores: _FakeRegistry) -> None:
        result = plugins_repo.create(CreatePluginCommand(key="   ", marketplace=""))
        assert not result.ok
        assert redirect_stores.last_install is None

    def test_create_rejects_duplicate_install(self, redirect_stores: _FakeRegistry) -> None:
        result = plugins_repo.create(
            CreatePluginCommand(key="github", marketplace="claude-plugins-official")
        )
        assert not result.ok

    def test_create_surfaces_installer_failure(self, redirect_stores: _FakeRegistry) -> None:
        redirect_stores.install_fails_with = "marketplace not registered"
        result = plugins_repo.create(CreatePluginCommand(key="ghost", marketplace="nowhere"))
        assert not result.ok
        assert result.message == "marketplace not registered"


class TestDelete:
    def test_delete_plan_is_deletable_for_normal_plugin(
        self, redirect_stores: _FakeRegistry
    ) -> None:
        plan = plugins_repo.delete_plan("github@claude-plugins-official")
        assert plan.deletable

    def test_delete_plan_unknown_plugin_is_not_deletable(
        self, redirect_stores: _FakeRegistry
    ) -> None:
        plan = plugins_repo.delete_plan("nope@nowhere")
        assert not plan.deletable

    def test_delete_calls_uninstall_and_removes_from_list(
        self, redirect_stores: _FakeRegistry
    ) -> None:
        plan = plugins_repo.delete_plan("github@claude-plugins-official")
        result = plugins_repo.delete("github@claude-plugins-official", plan.version)
        assert result.ok
        assert redirect_stores.last_uninstall == "github@claude-plugins-official"
        assert "github@claude-plugins-official" not in {p.id for p in plugins_repo.list()}

    def test_delete_stale_plan_version_is_rejected(self, redirect_stores: _FakeRegistry) -> None:
        plugins_repo.delete_plan("github@claude-plugins-official")
        result = plugins_repo.delete("github@claude-plugins-official", "stale-version")
        assert not result.ok
        assert redirect_stores.last_uninstall is None

    def test_delete_surfaces_installer_failure(self, redirect_stores: _FakeRegistry) -> None:
        redirect_stores.uninstall_fails_with = "in use"
        plan = plugins_repo.delete_plan("github@claude-plugins-official")
        result = plugins_repo.delete("github@claude-plugins-official", plan.version)
        assert not result.ok
        assert result.message == "in use"


class TestGovernableMarketplaces:
    def test_matches_safe_plugins_intersected_with_installed(
        self, monkeypatch: pytest.MonkeyPatch, redirect_stores: _FakeRegistry, tmp_path: Path
    ) -> None:
        import json

        cfg_dir = tmp_path / "claude-config"
        (cfg_dir / "plugins").mkdir(parents=True)
        (cfg_dir / "plugins" / "installed_plugins.json").write_text(
            json.dumps(
                {
                    "plugins": {
                        "github@claude-plugins-official": {},
                        "obsidian@claude-obsidian-marketplace": {},
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "default_claude_config_dir", lambda: cfg_dir)
        assert plugins_repo.governable_marketplaces() == {
            "claude-plugins-official",
            "claude-obsidian-marketplace",
        }
