"""OpenViking managed-local: installer + process supervisor + manager
(Wave 1, docs/plans/openviking-managed-local-2026-08-24/16_PHASES.md).

Keep `core.context_sources.openviking_adapter` as the ONLY HTTP client to
the sidecar — this package only ever installs/spawns/supervises the
`openviking-server` process itself, localhost-only, never vendoring
OpenViking's (AGPL) source into this (MIT) repo.
"""

from __future__ import annotations

from .manager import ManagerStatus, OpenVikingManager

__all__ = ["ManagerStatus", "OpenVikingManager"]
