"""core.providers — ProviderAdapter WRAP over spawn_engine.py's two spawn
branches (docs/v2/V2_IMPLEMENTATION_PLAN.md §2 Phase 2, epic #309 Phase 3).

Adapter contract suite parametrized across all 6 registered providers (plan
§4.1: "ทุก ProviderAdapter ต้องผ่าน test suite ชุดเดียวกัน (parametrized ข้าม
6 provider)")."""

from __future__ import annotations

import pytest

from agent_takkub.core.contracts.provider_adapter import ProviderAdapter
from agent_takkub.core.models.agent import AgentInstance
from agent_takkub.core.providers import ClaudeCliAdapter, CliProviderAdapter, adapter_for
from agent_takkub.core.providers.errors import ProviderAdapterNotWired
from agent_takkub.provider_spec import PROVIDER_REGISTRY

ALL_PROVIDER_IDS = tuple(PROVIDER_REGISTRY.keys())


def _instance() -> AgentInstance:
    return AgentInstance(id="inst-1", template_id="tpl-1")


# ── adapter_for() factory ────────────────────────────────────────────────


def test_adapter_for_claude_returns_claude_adapter():
    assert isinstance(adapter_for("claude"), ClaudeCliAdapter)


@pytest.mark.parametrize("provider_id", [p for p in ALL_PROVIDER_IDS if p != "claude"])
def test_adapter_for_non_claude_returns_cli_adapter(provider_id):
    adapter = adapter_for(provider_id)
    assert isinstance(adapter, CliProviderAdapter)
    assert adapter.provider_id() == provider_id


# ── contract suite, parametrized across all 6 providers ─────────────────


@pytest.mark.parametrize("provider_id", ALL_PROVIDER_IDS)
def test_adapter_satisfies_provider_adapter_protocol(provider_id):
    assert isinstance(adapter_for(provider_id), ProviderAdapter)


@pytest.mark.parametrize("provider_id", ALL_PROVIDER_IDS)
def test_provider_id_matches_registry_key(provider_id):
    assert adapter_for(provider_id).provider_id() == provider_id


@pytest.mark.parametrize("provider_id", ALL_PROVIDER_IDS)
def test_lifecycle_methods_raise_documented_not_wired(provider_id):
    adapter = adapter_for(provider_id)
    instance = _instance()
    with pytest.raises(ProviderAdapterNotWired):
        adapter.spawn(instance)
    with pytest.raises(ProviderAdapterNotWired):
        adapter.send(instance, "hello")
    with pytest.raises(ProviderAdapterNotWired):
        adapter.is_ready(instance)
    with pytest.raises(ProviderAdapterNotWired):
        adapter.terminate(instance)


# ── is_available(): real, reuses provider_config's source of truth ──────


def test_claude_is_available_delegates_to_provider_config(monkeypatch):
    import agent_takkub.provider_config as provider_config

    monkeypatch.setattr(provider_config, "_provider_available", lambda p: p == "claude")
    assert ClaudeCliAdapter().is_available() is True


def test_cli_adapter_is_available_delegates_to_provider_config(monkeypatch):
    import agent_takkub.provider_config as provider_config

    calls = []

    def fake(provider):
        calls.append(provider)
        return provider == "codex"

    monkeypatch.setattr(provider_config, "_provider_available", fake)
    assert CliProviderAdapter("codex").is_available() is True
    assert CliProviderAdapter("gemini").is_available() is False
    assert calls == ["codex", "gemini"]


def test_not_wired_error_names_provider_and_method():
    err = ProviderAdapterNotWired("codex", "spawn")
    assert "codex" in str(err)
    assert "spawn" in str(err)
