"""StaticRoutingPolicy — REUSE_VS_REWRITE_MATRIX.md §2: "Router |
`provider_config.effective_provider_for()` (static map + fallback) |
REPLACE ... เก็บไว้เป็น `StaticRoutingPolicy` ตัวหนึ่งใน router ใหม่เพื่อ
backward-compat". Delegates VERBATIM — no reimplementation of the
role->provider precedence/degrade logic — so this can never drift from the
function it wraps."""

from __future__ import annotations


class StaticRoutingPolicy:
    """`core.contracts.routing_policy.RoutingPolicy` over the existing
    static role->provider map + availability degrade."""

    def resolve(self, role: str, project: str | None = None) -> str:
        from agent_takkub.provider_config import effective_provider_for

        return effective_provider_for(role, project)
