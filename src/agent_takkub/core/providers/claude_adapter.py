"""ClaudeCliAdapter — WRAP over spawn_engine.py's claude spawn branch
(REUSE_VS_REWRITE_MATRIX.md §2: "Provider adapter ... claude branch
hardcode | WRAP -> NEW contract").

Scope decision (epic #309 Phase 3): the claude branch (spawn_engine.py,
roughly lines 1990-2900) builds its argv inline against live PaneState/env
mutation and ends by constructing a `PtySession` directly — it is not
already spec-driven the way the generic non-claude branch is (provider_spec
docstring: claude_spec's fields are "NOT wired into spawn_engine.py's claude
argv builder ... faithful documentation ... for a future phase"). Importing
`spawn_engine` from core would transitively pull in PyQt6 (`PtySession`/
`QTimer`) and trip the `core-is-bottom-layer` import-linter contract, so it
is never done — the pure part of the branch (final argv concatenation
order) is extracted instead, into `claude_plan.assemble_claude_argv`
(epic #309 2.0.0 Wave A). Same posture as the generic branch's
`plan.assemble_generic_argv`: extracted, golden-order-tested, importable —
but NOT wired into `spawn_engine.py`'s own live branch, which still builds
its argv inline. Rewiring that hot-path caller is a separate, later change.

What IS safe and real: `provider_id()`/`is_available()` call the exact
functions spawn_engine.py itself consults for claude (there is no
claude-specific availability check today — `provider_config._provider_available`
hardcodes `True` for claude — this adapter reuses that function rather than
re-deriving the answer, so the two can never drift). `spawn()`/`send()`/
`is_ready()`/`terminate()` are documented stubs — see `errors.py`.
"""

from __future__ import annotations

from collections.abc import Sequence

from agent_takkub.core.models.account import ProviderAccount
from agent_takkub.core.models.agent import AgentInstance
from agent_takkub.core.models.spawn_plan import SpawnPlan

from .errors import ProviderAdapterNotWired
from .plan import account_env_overrides

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

    def build_plan(
        self,
        *,
        argv: Sequence[str],
        base_env: dict[str, str],
        cwd: str,
        account: ProviderAccount | None = None,
    ) -> SpawnPlan:
        """Pure `SpawnPlan` for a claude spawn. `argv` is caller-built
        (spawn_engine.py's ~800-line claude branch stays the one place that
        assembles claude's argv — see module docstring / phase3-report.md
        §3; extracting it is a tracked gap, not this method's job) — this
        method's real, load-bearing contribution is turning *account* into
        the same `CLAUDE_CONFIG_DIR` override `pane_env.inject_user_profile_env`
        already applies from the legacy profile, so a future V2 account pool
        can win the same way without a second env-injection mechanism.
        """
        env = dict(base_env)
        env.update(account_env_overrides(_PROVIDER_ID, account))
        return SpawnPlan(provider_id=_PROVIDER_ID, argv=tuple(argv), env=env, cwd=cwd)

    def spawn(self, instance: AgentInstance) -> None:
        raise ProviderAdapterNotWired(_PROVIDER_ID, "spawn")

    def send(self, instance: AgentInstance, text: str) -> None:
        raise ProviderAdapterNotWired(_PROVIDER_ID, "send")

    def is_ready(self, instance: AgentInstance) -> bool:
        raise ProviderAdapterNotWired(_PROVIDER_ID, "is_ready")

    def terminate(self, instance: AgentInstance) -> None:
        raise ProviderAdapterNotWired(_PROVIDER_ID, "terminate")
