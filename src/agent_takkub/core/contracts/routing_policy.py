"""Contract a routing policy must satisfy (Phase 3, epic #309) — `Router`
(`core.routing.router`) picks one policy and delegates `resolve()` to it.
`StaticRoutingPolicy` (`core.routing.policy`) is the first, and for this
phase the ONLY, real implementation — reproducing
`provider_config.effective_provider_for()`.

#323 alignment note: this Phase 3 contract resolves PROVIDER only — model
and reasoning-effort are not (yet) part of it. `takkub assign --effort` is
therefore implemented as a `PaneState.effort_override` field resolved
locally in `spawn_engine._resolve_teammate_effort`, the exact same shape
`--model`/`PaneState.model_override` already used BEFORE this contract
existed and still uses today (neither call site consults `Router`). Folding
effort in here without also folding model in would be an inconsistent
routing surface — one provider decision made two different ways. The task→
(provider, model, effort) tuple `RoutingPolicy.resolve()` could grow into is
tracked as a real widening of this Protocol for a later V2 phase, not an
oversight of this change; do it for model and effort together."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class RoutingPolicy(Protocol):
    def resolve(self, role: str, project: str | None = None) -> str: ...
