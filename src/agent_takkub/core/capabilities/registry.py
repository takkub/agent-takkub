"""CapabilityRegistry (Phase 5b, epic #309 Capability Hub).

WRAPs the three modules that already decide "what capabilities does this
role/pane get" (matrix §3):

  - `skill_policy` — which Skill Matrix entries a role gets, resolved
    against the real `SKILL.md` files `skill_scan.scan_skills` finds.
  - `pane_tools_policy` — which MCP servers / plugins a role gets.
  - `mcp_bridge` — translates that MCP set into each provider's own wire
    format (`--mcp-config`, `-c mcp_servers.*=`, ...).

None of their logic changes here — this is a read façade so a caller
(and later phases) can ask one object "what does role X get" instead of
importing three separate policy modules, each keyed by
`core.models.capability.CapabilityScope`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_takkub import mcp_bridge, pane_tools_policy, skill_policy, skill_scan
from agent_takkub.core.models.capability import CapabilityScope, Skill


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    """Everything one role resolves to, in one call — the read-side of the
    Capability Hub concept (blueprint `04_CAPABILITY_HUB.md` §1): Skills +
    MCP server names + plugin names, already scoped/filtered for `role`."""

    role: str
    scope: CapabilityScope
    skills: tuple[Skill, ...]
    mcp_server_names: tuple[str, ...]
    plugin_names: tuple[str, ...]


class CapabilityRegistry:
    """Read-only façade over `skill_policy` + `pane_tools_policy` +
    `mcp_bridge`. Every method mirrors what `spawn_engine.py` already
    calls directly today — this class changes *how many imports* a caller
    needs, not *what* gets resolved."""

    def resolve_skills(self, role: str, roots: list[Path]) -> tuple[Skill, ...]:
        """Skills the Skill Matrix assigned to `role` that actually exist
        under `roots` — same filtering `skill_policy.render_skill_appendix`
        does, exposed as `core.models.capability.Skill` records instead of
        rendered markdown."""
        assigned = set(skill_policy.effective_skills(role))
        if not assigned:
            return ()
        found = skill_scan.scan_skills(roots)
        return tuple(
            Skill(
                id=s.name,
                name=s.name,
                description=s.description,
                scope=CapabilityScope.PROJECT,
            )
            for s in found
            if s.name in assigned
        )

    def resolve_mcp_server_names(self, role: str) -> frozenset[str] | None:
        """`None` = no cockpit MCP policy for this role (legacy
        passthrough); see `pane_tools_policy.effective_mcps`."""
        return pane_tools_policy.effective_mcps(role)

    def resolve_plugin_names(self, role: str) -> frozenset[str] | None:
        """`None` = no cockpit plugin policy for this role; see
        `pane_tools_policy.effective_plugins`."""
        return pane_tools_policy.effective_plugins(role)

    def mcp_argv_for_provider(
        self,
        provider_name: str,
        role: str,
        shard_idx: int | None,
        project_ns: str,
        **kwargs: object,
    ) -> list[str]:
        """Extra argv tokens for *provider_name*'s MCP injection — thin
        pass-through to `mcp_bridge.mcp_argv_for_provider` (can raise
        `mcp_bridge.McpResolutionError` for the codex variant; see that
        function's docstring — this call must not swallow it)."""
        return mcp_bridge.mcp_argv_for_provider(
            provider_name, role, shard_idx, project_ns, **kwargs
        )

    def snapshot(
        self,
        role: str,
        roots: list[Path],
        *,
        scope: CapabilityScope = CapabilityScope.PROJECT,
    ) -> CapabilitySnapshot:
        """Everything `role` gets, in one call — skills + MCP/plugin
        names. Does not resolve provider argv (that needs a provider
        name); call `mcp_argv_for_provider` separately for that."""
        skills = self.resolve_skills(role, roots)
        mcps = self.resolve_mcp_server_names(role) or frozenset()
        plugins = self.resolve_plugin_names(role) or frozenset()
        return CapabilitySnapshot(
            role=role,
            scope=scope,
            skills=skills,
            mcp_server_names=tuple(sorted(mcps)),
            plugin_names=tuple(sorted(plugins)),
        )
