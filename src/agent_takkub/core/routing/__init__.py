"""Router + routing policy (Phase 3, epic #309)."""

from __future__ import annotations

from .facade import effective_model_for_v2, effective_provider_for_v2
from .flag import v2_router_enabled
from .policy import StaticRoutingPolicy
from .router import Router

__all__ = [
    "Router",
    "StaticRoutingPolicy",
    "effective_model_for_v2",
    "effective_provider_for_v2",
    "v2_router_enabled",
]
