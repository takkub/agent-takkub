"""Contract a routing policy must satisfy (Phase 3, epic #309) — `Router`
(`core.routing.router`) picks one policy and delegates `resolve()` to it.
`StaticRoutingPolicy` (`core.routing.policy`) is the first, and for this
phase the ONLY, real implementation — reproducing
`provider_config.effective_provider_for()`."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class RoutingPolicy(Protocol):
    def resolve(self, role: str, project: str | None = None) -> str: ...
