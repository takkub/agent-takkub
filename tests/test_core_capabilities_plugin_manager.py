"""core.capabilities.plugin_manager.PluginManager — claude-only plugin
backend, explicit gap for every other provider (#103, Phase 5b epic
#309)."""

from __future__ import annotations

import pytest

from agent_takkub import plugin_installer
from agent_takkub.core.capabilities.plugin_manager import (
    NO_BACKEND_PROVIDERS,
    PluginBackendGapError,
    PluginManager,
)


@pytest.fixture
def manager() -> PluginManager:
    return PluginManager()


@pytest.mark.parametrize("provider", sorted(NO_BACKEND_PROVIDERS))
def test_install_raises_gap_error_for_every_non_claude_provider(
    manager: PluginManager, provider: str
) -> None:
    with pytest.raises(PluginBackendGapError):
        manager.install(provider, "some-plugin")


@pytest.mark.parametrize("provider", sorted(NO_BACKEND_PROVIDERS))
def test_uninstall_raises_gap_error_for_every_non_claude_provider(
    manager: PluginManager, provider: str
) -> None:
    with pytest.raises(PluginBackendGapError):
        manager.uninstall(provider, "some-plugin")


def test_install_delegates_to_plugin_installer_for_claude(
    monkeypatch: pytest.MonkeyPatch, manager: PluginManager
) -> None:
    monkeypatch.setattr(plugin_installer, "install_by_id", lambda plugin_id: (True, "ok"))

    ok, msg = manager.install("claude", "code-review")

    assert (ok, msg) == (True, "ok")


def test_uninstall_delegates_to_plugin_installer_for_claude(
    monkeypatch: pytest.MonkeyPatch, manager: PluginManager
) -> None:
    monkeypatch.setattr(plugin_installer, "uninstall_plugin", lambda plugin_id: (True, "removed"))

    ok, msg = manager.uninstall("claude", "code-review")

    assert (ok, msg) == (True, "removed")


def test_list_recommended_flags_installed_from_disk(
    monkeypatch: pytest.MonkeyPatch, manager: PluginManager
) -> None:
    first_key = plugin_installer.RECOMMENDED[0].key
    monkeypatch.setattr(plugin_installer, "installed_on_disk", lambda home=None: {first_key})

    statuses = manager.list_recommended()

    assert len(statuses) == len(plugin_installer.RECOMMENDED)
    by_key = {s.key: s for s in statuses}
    assert by_key[first_key].installed is True
