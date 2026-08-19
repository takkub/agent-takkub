"""Capability Hub (Phase 5, epic #309) — provider-independent Skill / MCP /
Plugin / Tool layer per blueprint `04_CAPABILITY_HUB.md`.

WRAP, not REPLACE (docs/v2/REUSE_VS_REWRITE_MATRIX.md §3 "Capability
Layer"): every module here calls the existing skill_scan / skill_policy /
pane_tools_policy / mcp_bridge / pane_guard / plugin_installer — none of
their logic changes. This package just gives that scattered policy one
front door, keyed by `core.models.capability.CapabilityScope`.
"""

from __future__ import annotations
