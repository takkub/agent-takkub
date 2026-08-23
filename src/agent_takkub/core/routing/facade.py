"""The façade calls spawn_engine.py's provider/model resolution call sites
are patched to go through (plan §2 Phase 2 / §1.3 Wave C:
"จุดเชื่อมเดียว ... 1 façade call, fail-open").

`effective_provider_for_v2` — spawn()'s single `effective_provider_for` call
site (line ~1463).
Flag OFF (`TAKKUB_V2_ROUTER=0`): calls `provider_config.effective_provider_for` directly
— zero `Router` involvement, byte-identical to pre-#309 behavior (proof:
`tests/test_core_routing.py`'s flag-off parity test).
Flag ON: goes through `Router(StaticRoutingPolicy())`, which for this phase
resolves to the exact same function — any exception anywhere in that path
falls straight back to the direct call (fail-open, plan §0 rule 4) instead
of ever blocking a spawn.

`effective_model_for_v2` — spawn_engine's `role_models.model_for(role,
provider) or provider_models.model_for(provider)` call sites (Wave C, plan
§1.3). Same flag-off/flag-on/fail-open shape as above, over the V2 model
catalog instead of the provider map.
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


def effective_model_for_v2(role: str, provider: str, project: str | None = None) -> str | None:
    """spawn_engine's model-pin call sites (`role_models.model_for(role,
    provider) or provider_models.model_for(provider)`) are patched to go
    through this one façade call, same shape/fail-open guarantee as
    `effective_provider_for_v2` above.

    Flag OFF: calls the V1 functions directly — zero `Router` involvement,
    byte-identical to pre-Wave-C behavior.
    Flag ON: goes through `Router().effective_model_for`, which reads the V2
    model catalog; any exception anywhere in that path falls straight back
    to the direct V1 calls (fail-open) instead of ever blocking a spawn.
    """

    def _direct() -> str | None:
        from agent_takkub.provider_models import model_for as _provider_model_for
        from agent_takkub.role_models import model_for as _role_model_for

        return _role_model_for(role, provider) or _provider_model_for(provider)

    if not v2_router_enabled():
        return _direct()
    try:
        return Router().effective_model_for(role, provider, project)
    except Exception:
        _log.exception(
            "core.routing.Router failed resolving model for role=%r provider=%r project=%r — "
            "falling back to role_models/provider_models.model_for (fail-open)",
            role,
            provider,
            project,
        )
        return _direct()
