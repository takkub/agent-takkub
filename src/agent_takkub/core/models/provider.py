"""Provider vocabulary — distinct from the existing engine-layer
`agent_takkub.provider_spec.ProviderSpec` (spawn argv / ready-marker wiring).
`ProviderDefinition` is the V2 domain object a future `ProviderAdapter`
(core/contracts/provider_adapter.py) is built against; it does not replace
`ProviderSpec` in Phase 1 — see REUSE_VS_REWRITE_MATRIX.md (`ProviderSpec` is
EXTEND, not REPLACE).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Transport(StrEnum):
    """How the cockpit talks to one provider's CLI."""

    PTY = "pty"
    API = "api"


class ProviderFeature(StrEnum):
    """Optional capability flags — mirrors `ProviderSpec.supports_*`/`mcp_adapter_variant`."""

    RESUME = "resume"
    MIRROR = "mirror"
    MCP = "mcp"
    BROWSER_PROFILES = "browser_profiles"


@dataclass(frozen=True, slots=True)
class ProviderDefinition:
    """One CLI provider (claude/codex/gemini-agy/opencode/kimi/cursor/…)."""

    id: str
    name: str
    display_name: str = ""
    transport: Transport = Transport.PTY
    features: frozenset[ProviderFeature] = field(default_factory=frozenset)
    user_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None
