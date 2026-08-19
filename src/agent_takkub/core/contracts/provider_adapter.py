"""Contract every V2 provider adapter must satisfy (Phase 2 wires the first
implementation over the existing spawn_engine PTY branch — behind a flag,
old path unchanged when off).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_takkub.core.models.agent import AgentInstance


@runtime_checkable
class ProviderAdapter(Protocol):
    def provider_id(self) -> str: ...

    def is_available(self) -> bool: ...

    def spawn(self, instance: AgentInstance) -> None: ...

    def send(self, instance: AgentInstance, text: str) -> None: ...

    def is_ready(self, instance: AgentInstance) -> bool: ...

    def terminate(self, instance: AgentInstance) -> None: ...
