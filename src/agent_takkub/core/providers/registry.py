"""Factory: `PROVIDER_REGISTRY` id -> `ProviderAdapter` instance."""

from __future__ import annotations

from agent_takkub.core.contracts.provider_adapter import ProviderAdapter

from .claude_adapter import ClaudeCliAdapter
from .cli_adapter import CliProviderAdapter


def adapter_for(provider_id: str) -> ProviderAdapter:
    """Return the `ProviderAdapter` for `provider_id`.

    Does not validate `provider_id` against `PROVIDER_REGISTRY` — that
    check belongs to the caller (mirrors `CliProviderAdapter.is_available()`
    itself deferring to `provider_config._provider_available`, which
    tolerates an unregistered id by returning False rather than raising).
    """
    if provider_id == "claude":
        return ClaudeCliAdapter()
    return CliProviderAdapter(provider_id)
