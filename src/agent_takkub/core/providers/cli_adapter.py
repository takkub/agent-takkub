"""CliProviderAdapter — WRAP over spawn_engine.py's generic non-claude spawn
branch (REUSE_VS_REWRITE_MATRIX.md §2: "generic non-claude branch (#103
Phase 1) | WRAP -> NEW contract"), parametrized by any `PROVIDER_REGISTRY`
entry (codex/gemini/opencode/kimi/cursor/...).

Same scope decision as `claude_adapter.py`: `provider_id()`/`is_available()`
call the real, already-pure functions the generic branch itself uses
(`ProviderSpec.custom_discovery_fn`, `provider_config._provider_available`)
so this adapter can never disagree with spawn_engine.py about whether a
provider is usable right now. `spawn()`/`send()`/`is_ready()`/`terminate()`
stay documented stubs — the branch's actual PTY/env-mutation work needs
PyQt6 (`spawn_engine.py`'s `PtySession`), which `core-is-bottom-layer`
forbids this module from reaching even transitively.

Unlike the claude branch, the generic branch genuinely IS spec-driven
already (issue #103 Phase 1: "One spec-driven path replaces the old
hand-written GEMINI and CODEX branches") — `is_available()` below is real,
tested, reusable logic, not a documentation-only mirror.
"""

from __future__ import annotations

from agent_takkub.core.models.agent import AgentInstance

from .errors import ProviderAdapterNotWired


class CliProviderAdapter:
    """Structural `core.contracts.provider_adapter.ProviderAdapter` for one
    generic (non-claude) `PROVIDER_REGISTRY` entry."""

    def __init__(self, provider_id: str) -> None:
        self._provider_id = provider_id

    def provider_id(self) -> str:
        return self._provider_id

    def is_available(self) -> bool:
        from agent_takkub.provider_config import _provider_available

        return _provider_available(self._provider_id)

    def spawn(self, instance: AgentInstance) -> None:
        raise ProviderAdapterNotWired(self._provider_id, "spawn")

    def send(self, instance: AgentInstance, text: str) -> None:
        raise ProviderAdapterNotWired(self._provider_id, "send")

    def is_ready(self, instance: AgentInstance) -> bool:
        raise ProviderAdapterNotWired(self._provider_id, "is_ready")

    def terminate(self, instance: AgentInstance) -> None:
        raise ProviderAdapterNotWired(self._provider_id, "terminate")
