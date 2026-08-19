"""Router — the future home of quota/cooldown/health/concurrency-aware
provider routing (REUSE_VS_REWRITE_MATRIX.md §2: "ของเดิมเป็น mapping ไม่ใช่
routing (ไม่มี quota/cooldown/health/concurrency)"). Phase 3 wires exactly
one policy (`StaticRoutingPolicy`) so the router itself is real and
testable today, without changing what it resolves to."""

from __future__ import annotations

from agent_takkub.core.contracts.routing_policy import RoutingPolicy

from .policy import StaticRoutingPolicy


class Router:
    def __init__(self, policy: RoutingPolicy | None = None) -> None:
        self._policy: RoutingPolicy = policy or StaticRoutingPolicy()

    def effective_provider_for(self, role: str, project: str | None = None) -> str:
        return self._policy.resolve(role, project)
