"""V1 JSON → V2 domain object readers (plan §5.4): every V1 config file gets
a reader here that converts it into a V2 object *at read time*, so Core V2
can work before any disk migration lands (real migration is Phase 8).

Phase 1 ships the framework (`read_json`) plus one concrete reader
(`read_role_provider_routing_policy`, over `agent_takkub.provider_config`'s
`role-providers.json`) to prove the pattern. The rest land alongside their
owning phase.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


def read_json(path: Path) -> dict:
    """Missing/invalid V1 file reads as empty, never raises — fail-open (plan §0.4)."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


@dataclass(frozen=True, slots=True)
class RoleProviderRoutingPolicy:
    """V2 read of V1 `role-providers.json` — role name -> provider id override."""

    overrides: dict[str, str] = field(default_factory=dict)


def read_role_provider_routing_policy(path: Path) -> RoleProviderRoutingPolicy:
    return RoleProviderRoutingPolicy(overrides=read_json(path))
