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

    def effective_model_for(
        self, role: str, provider: str, project: str | None = None
    ) -> str | None:
        """Model pin lookup (#504 cut half: `role_models.py`/
        `provider_models.py` read/write their V2 target directly now, so
        there is exactly one store left to consult here — no V1-vs-V2
        shadow-read, no `model_pin_v2_drift` telemetry, no
        `TAKKUB_V2_AUTHORITY` gate).

        Deliberately NOT routed through `RoutingPolicy.resolve()` —
        `core.contracts.routing_policy`'s own docstring flags folding model
        (and effort) into that Protocol as a real widening for a later V2
        phase, not something to do half-way here.

        `project` is accepted (not yet consulted — the role/provider model
        pin store carries no project scoping either) purely to keep this
        façade's shape consistent with `effective_provider_for`.
        """
        from agent_takkub.provider_models import model_for as _provider_model_for
        from agent_takkub.role_models import model_for as _role_model_for

        return _role_model_for(role, provider) or _provider_model_for(provider)
