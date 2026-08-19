"""ClaudeCliAdapter — WRAP over spawn_engine.py's claude spawn branch
(REUSE_VS_REWRITE_MATRIX.md §2: "Provider adapter ... claude branch
hardcode | WRAP -> NEW contract").

Scope decision (epic #309 Phase 3): the claude branch (spawn_engine.py,
roughly lines 1990-2900) builds its argv inline against live PaneState/env
mutation and ends by constructing a `PtySession` directly — it is not
already spec-driven the way the generic non-claude branch is (provider_spec
docstring: claude_spec's fields are "NOT wired into spawn_engine.py's claude
argv builder ... faithful documentation ... for a future phase"). Lifting
that ~800-line block into something `agent_takkub.core` can import would
mean either (a) extracting it — risky for a single Phase-3 task against a
module backed by the bulk of the suite's 7,033 tests, explicitly out of
scope per this phase's "ห้ามแก้ logic ใน branch เดิม" instruction — or
(b) importing `spawn_engine` from core, which transitively pulls in PyQt6
(`PtySession`/`QTimer`) and trips the `core-is-bottom-layer` import-linter
contract. Neither is acceptable this phase.

What IS safe and real: `provider_id()`/`is_available()` call the exact
functions spawn_engine.py itself consults for claude (there is no
claude-specific availability check today — `provider_config._provider_available`
hardcodes `True` for claude — this adapter reuses that function rather than
re-deriving the answer, so the two can never drift). `spawn()`/`send()`/
`is_ready()`/`terminate()` are documented stubs — see `errors.py`.
"""

from __future__ import annotations

from agent_takkub.core.models.agent import AgentInstance

from .errors import ProviderAdapterNotWired

_PROVIDER_ID = "claude"


class ClaudeCliAdapter:
    """Structural `core.contracts.provider_adapter.ProviderAdapter` for claude."""

    def provider_id(self) -> str:
        return _PROVIDER_ID

    def is_available(self) -> bool:
        # claude is always considered available — provider_config's own
        # source of truth (it's the cockpit's baseline; if the binary is
        # genuinely missing the spawn fails far louder elsewhere).
        from agent_takkub.provider_config import _provider_available

        return _provider_available(_PROVIDER_ID)

    def spawn(self, instance: AgentInstance) -> None:
        raise ProviderAdapterNotWired(_PROVIDER_ID, "spawn")

    def send(self, instance: AgentInstance, text: str) -> None:
        raise ProviderAdapterNotWired(_PROVIDER_ID, "send")

    def is_ready(self, instance: AgentInstance) -> bool:
        raise ProviderAdapterNotWired(_PROVIDER_ID, "is_ready")

    def terminate(self, instance: AgentInstance) -> None:
        raise ProviderAdapterNotWired(_PROVIDER_ID, "terminate")
