"""ProviderSpec's Core V2 field group (§16, epic #309 Phase 3) —
REUSE_VS_REWRITE_MATRIX.md §2: "Provider definition | EXTEND | เติม
transport/auth kinds/adapter id/compatibility range". Purely additive:
every registered provider must keep its documented default until something
explicitly sets otherwise."""

from __future__ import annotations

import pytest

from agent_takkub.provider_spec import PROVIDER_REGISTRY


@pytest.mark.parametrize("provider_id", list(PROVIDER_REGISTRY.keys()))
def test_v2_fields_default_untouched_for_every_registered_provider(provider_id):
    spec = PROVIDER_REGISTRY[provider_id]
    assert spec.transport == "cli"
    assert spec.auth_kinds == ()
    assert spec.adapter_id == ""
    assert spec.compat_range == ""
