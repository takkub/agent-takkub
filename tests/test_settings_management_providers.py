"""Characterization + CRUD tests for settings_management's Providers slice.

Two layers per SPEC.md §Providers: spec definition (BUILT-IN, read-only,
from ``PROVIDER_REGISTRY``) vs operational override (enabled/disabled via
``provider_state`` — the only writable field, `claude` excluded).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_takkub import provider_config, provider_models, provider_state
from agent_takkub.settings_management.commands import UpdateProviderCommand
from agent_takkub.settings_management.models import Ownership
from agent_takkub.settings_management.repositories import providers as providers_repo


@pytest.fixture(autouse=True)
def redirect_stores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(provider_state, "_PATH", tmp_path / "disabled-providers.json")
    monkeypatch.setattr(provider_config, "_CONFIG_PATH", tmp_path / "role-providers.json")
    monkeypatch.setattr(provider_config, "_BASE_DIR", tmp_path)
    monkeypatch.setattr(provider_models, "_PATH", tmp_path / "provider-models.json")
    yield tmp_path


class TestList:
    def test_list_includes_all_registry_providers(self) -> None:
        names = {p.name for p in providers_repo.list()}
        assert names == {"claude", "codex", "gemini", "opencode", "kimi", "cursor"}

    def test_list_query_filters_by_name(self) -> None:
        # "cod" would match both codex and open"cod"e — use an unambiguous
        # fragment per provider.
        assert {p.name for p in providers_repo.list(query="codex")} == {"codex"}
        assert {p.name for p in providers_repo.list(query="openc")} == {"opencode"}

    def test_claude_is_required_and_always_enabled(self) -> None:
        by_name = {p.name: p for p in providers_repo.list()}
        assert by_name["claude"].required is True
        assert by_name["claude"].enabled is True

    def test_codex_gemini_are_not_required(self) -> None:
        by_name = {p.name: p for p in providers_repo.list()}
        assert by_name["codex"].required is False
        assert by_name["gemini"].required is False


class TestGet:
    def test_get_unknown_provider_raises(self) -> None:
        with pytest.raises(providers_repo.ProviderNotFoundError):
            providers_repo.get("bogus")

    def test_get_returns_built_in_ownership(self) -> None:
        detail = providers_repo.get("codex")
        assert detail.ownership is Ownership.BUILT_IN

    def test_get_reflects_disabled_state(self) -> None:
        provider_state.set_disabled("codex", True)
        detail = providers_repo.get("codex")
        assert detail.enabled is False

    def test_get_lists_assigned_roles(self) -> None:
        provider_config.save_role_overrides({"backend": "codex"})
        detail = providers_repo.get("codex")
        assert "backend" in detail.assigned_roles

    def test_get_forced_role_shows_in_assigned_roles(self) -> None:
        detail = providers_repo.get("codex")
        assert "codex" in detail.assigned_roles  # role's own CLI identity is forced codex


class TestCapabilities:
    def test_claude_can_be_updated_model_only(self) -> None:
        # claude's `enabled` flag can never be flipped (required provider),
        # but its per-provider `model` override is still writable — so
        # can_update stays True (update() itself rejects an enabled flip).
        cap = providers_repo.capabilities("claude")
        assert cap.can_update is True
        assert cap.reason

    def test_codex_can_be_updated(self) -> None:
        cap = providers_repo.capabilities("codex")
        assert cap.can_update is True

    def test_no_provider_can_be_created_or_deleted(self) -> None:
        cap = providers_repo.capabilities("codex")
        assert cap.can_create is False
        assert cap.can_delete is False


class TestUpdate:
    def test_update_toggles_enabled_off(self) -> None:
        result = providers_repo.update("codex", UpdateProviderCommand(enabled=False))
        assert result.ok
        assert provider_state.is_disabled("codex") is True

    def test_update_toggles_enabled_on(self) -> None:
        provider_state.set_disabled("codex", True)
        result = providers_repo.update("codex", UpdateProviderCommand(enabled=True))
        assert result.ok
        assert provider_state.is_disabled("codex") is False

    def test_update_rejects_claude(self) -> None:
        result = providers_repo.update("claude", UpdateProviderCommand(enabled=False))
        assert not result.ok

    def test_update_rejects_unknown_provider(self) -> None:
        result = providers_repo.update("bogus", UpdateProviderCommand(enabled=False))
        assert not result.ok


class TestModelFlagSupported:
    def test_supported_for_all_registered_providers(self) -> None:
        # Every registered provider declares model_flag — codex's was verified
        # against the installed binary (`codex --help`: `-m, --model <MODEL>`).
        for name in ("claude", "codex", "gemini", "opencode", "kimi", "cursor"):
            assert providers_repo.get(name).model_flag_supported is True, name


class TestModel:
    def test_get_shows_empty_model_by_default(self) -> None:
        assert providers_repo.get("gemini").model == ""

    def test_list_shows_configured_model(self) -> None:
        provider_models.set_model("gemini", "gemini-2.5-pro")
        by_name = {p.name: p for p in providers_repo.list()}
        assert by_name["gemini"].model == "gemini-2.5-pro"

    def test_update_sets_model(self) -> None:
        result = providers_repo.update(
            "gemini", UpdateProviderCommand(enabled=True, model="gemini-2.5-pro")
        )
        assert result.ok
        assert provider_models.model_for("gemini") == "gemini-2.5-pro"
        assert providers_repo.get("gemini").model == "gemini-2.5-pro"

    def test_update_clears_model_with_empty_string(self) -> None:
        provider_models.set_model("gemini", "gemini-2.5-pro")
        result = providers_repo.update("gemini", UpdateProviderCommand(enabled=True, model=""))
        assert result.ok
        assert provider_models.model_for("gemini") is None

    def test_update_model_none_leaves_it_untouched(self) -> None:
        provider_models.set_model("gemini", "gemini-2.5-pro")
        result = providers_repo.update("gemini", UpdateProviderCommand(enabled=True, model=None))
        assert result.ok
        assert provider_models.model_for("gemini") == "gemini-2.5-pro"

    def test_update_model_on_unknown_provider_fails(self) -> None:
        result = providers_repo.update("bogus", UpdateProviderCommand(enabled=True, model="x"))
        assert not result.ok

    def test_claude_can_update_model_even_though_enabled_is_locked(self) -> None:
        result = providers_repo.update("claude", UpdateProviderCommand(enabled=True, model="opus"))
        assert result.ok
        assert provider_models.model_for("claude") == "opus"

    def test_claude_enabled_flip_still_rejected_alongside_model_set(self) -> None:
        result = providers_repo.update("claude", UpdateProviderCommand(enabled=False, model="opus"))
        assert not result.ok
        # Rejected before the model write happens — no partial apply.
        assert provider_models.model_for("claude") is None
