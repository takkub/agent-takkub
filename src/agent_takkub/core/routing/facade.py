"""The ONE façade call spawn_engine.py's single `effective_provider_for`
call site (`spawn()`, line ~1463) is patched to go through (plan §2 Phase 2:
"จุดเชื่อมเดียว ... 1 façade call, fail-open").

Flag OFF (`TAKKUB_V2_ROUTER=0`): calls `provider_config.effective_provider_for` directly
— zero `Router` involvement, byte-identical to pre-#309 behavior (proof:
`tests/test_core_routing.py`'s flag-off parity test).
Flag ON: goes through `Router(StaticRoutingPolicy())`, which for this phase
resolves to the exact same function — any exception anywhere in that path
falls straight back to the direct call (fail-open, plan §0 rule 4) instead
of ever blocking a spawn.
"""

from __future__ import annotations

import logging

from .flag import v2_router_enabled
from .router import Router

_log = logging.getLogger(__name__)


def effective_provider_for_v2(role: str, project: str | None = None) -> str:
    from agent_takkub.provider_config import effective_provider_for as _direct

    if not v2_router_enabled():
        return _direct(role, project)
    try:
        return Router().effective_provider_for(role, project)
    except Exception:
        _log.exception(
            "core.routing.Router failed resolving role=%r project=%r — falling back to "
            "provider_config.effective_provider_for (fail-open)",
            role,
            project,
        )
        return _direct(role, project)
